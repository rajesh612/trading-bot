"""
Backtest data layer: fetch 5-min bars (cached), daily bars, VIX regimes.
All network I/O lives here. No simulation logic.
"""
import hashlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

import config
from utils.constants import ET
from utils.http import yahoo_get

logger = logging.getLogger(__name__)

_FETCH_SYMBOL_BATCH = 25
_FETCH_DAY_CHUNK    = 30   # trading days per time-window chunk
_CACHE_DIR          = config.BASE_DIR / "data" / "cache"


# ── Watch-list selection ─────────────────────────────────────────────────────

def select_watch_list_for_day(
    daily_bars: dict[str, pd.DataFrame],
    day: datetime,
    n: int = 25,
) -> list[str]:
    """
    Rank tickers by ATR% using daily bars up to (but not including) `day`.
    No network calls — slices the pre-fetched daily_bars dict in memory.
    """
    # Use UTC midnight of the day's calendar date so daily bars with a
    # midnight-UTC timestamp on `day` are correctly excluded regardless of ET offset.
    utc = ZoneInfo("UTC")
    day_utc_midnight = datetime(day.year, day.month, day.day, tzinfo=utc)
    lookback_cutoff  = day_utc_midnight - timedelta(days=22)  # ~20 trading-day buffer

    scores: dict[str, float] = {}
    for sym, df in daily_bars.items():
        if df.empty:
            continue
        idx = df.index
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
            df = df.copy()
            df.index = idx

        window = df[
            (df.index >= lookback_cutoff) &
            (df.index <  day_utc_midnight)
        ]
        if len(window) < 5:
            continue

        prev_close = window["close"].shift(1)
        tr = pd.concat([
            window["high"] - window["low"],
            (window["high"] - prev_close).abs(),
            (window["low"]  - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr   = tr.ewm(com=13, min_periods=5).mean().iloc[-1]
        scores[sym] = atr

    ranked = sorted(scores, key=lambda s: scores[s], reverse=True)
    watch  = ranked[:n] if len(ranked) >= n else list(ranked)
    logger.info(
        "  Watch list for %s (%d): %s%s",
        day.date(), len(watch),
        ", ".join(watch[:10]), "…" if len(watch) > 10 else "",
    )
    return watch


# ── Daily bars — single fetch for entire range ───────────────────────────────

def fetch_daily_bars_range(
    symbols: list[str], start: datetime, end: datetime
) -> dict[str, pd.DataFrame]:
    """
    Fetch daily OHLCV bars for all symbols in one Alpaca request.
    Call once per backtest run; slice per day with select_watch_list_for_day().
    """
    client = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
    logger.info("Fetching daily bars %s → %s for %d tickers…", start.date(), end.date(), len(symbols))

    sym_batches = [symbols[i:i + _FETCH_SYMBOL_BATCH] for i in range(0, len(symbols), _FETCH_SYMBOL_BATCH)]
    result: dict[str, pd.DataFrame] = {}

    for batch in sym_batches:
        try:
            req = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                feed="iex",
            )
            bars  = client.get_stock_bars(req)
            df_all = bars.df
            if df_all is None or df_all.empty:
                continue
            if isinstance(df_all.index, pd.MultiIndex):
                for sym in batch:
                    try:
                        df = df_all.xs(sym, level="symbol")
                        if not df.empty:
                            result[sym] = df
                    except KeyError:
                        pass
            elif batch:
                result[batch[0]] = df_all
        except Exception as exc:
            logger.warning("Daily bar fetch failed for batch %s…: %s", batch[:3], exc)

    logger.info("  Daily bars received for %d symbols.", len(result))
    return result


# ── VIX regime — single fetch for entire range ───────────────────────────────

def _classify_vix_day(
    past_vix: list[tuple[int, float]],
    past_spy: list[tuple[int, float]],
) -> str:
    """Classify one day's regime from pre-fetched VIX + SPY pair lists."""
    if not past_vix:
        return "NORMAL"

    _, vix_current = past_vix[-1]
    vix_prev  = past_vix[-2][1] if len(past_vix) >= 2 else vix_current
    vix_rising = vix_current > vix_prev

    lookback    = min(90, len(past_vix) - 1)
    vix_avg_90d = (
        sum(c for _, c in past_vix[-lookback - 1:-1]) / lookback
        if lookback > 0 else vix_current
    )

    spy_change = 0.0
    if len(past_spy) >= 2:
        spy_change = (past_spy[-1][1] - past_spy[-2][1]) / past_spy[-2][1] * 100

    if vix_current > vix_avg_90d and vix_rising and spy_change < -1.0:
        return "CRISIS"
    if vix_current > vix_avg_90d:
        return "HIGH_VOL"
    return "NORMAL"


def fetch_vix_regimes(trading_days: list[datetime]) -> dict[str, str]:
    """
    Fetch VIX + SPY once, then classify every day in trading_days.
    Returns {date_str → "NORMAL" | "HIGH_VOL" | "CRISIS"}.
    """
    def _yahoo_pairs(symbol: str, range_str: str) -> list[tuple[int, float]]:
        data = yahoo_get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            f"?interval=1d&range={range_str}"
        )
        result = data["chart"]["result"][0]
        ts_raw = result["timestamp"]
        cl_raw = result["indicators"]["quote"][0]["close"]
        return [(t, c) for t, c in zip(ts_raw, cl_raw) if c is not None]

    try:
        vix_pairs = _yahoo_pairs("%5EVIX", "200d")
        spy_pairs = _yahoo_pairs("SPY",    "200d")
        logger.info("  VIX/SPY fetched: %d VIX bars, %d SPY bars", len(vix_pairs), len(spy_pairs))
    except Exception as exc:
        logger.warning("VIX fetch failed (%s) — defaulting all days to NORMAL", exc)
        return {d.strftime("%Y-%m-%d"): "NORMAL" for d in trading_days}

    regimes: dict[str, str] = {}
    for day in trading_days:
        date_str = day.strftime("%Y-%m-%d")
        day_ts   = day.timestamp()
        past_vix = [(t, c) for t, c in vix_pairs if t < day_ts]
        past_spy = [(t, c) for t, c in spy_pairs if t < day_ts]
        try:
            regimes[date_str] = _classify_vix_day(past_vix, past_spy)
        except Exception as exc:
            logger.warning("VIX classify failed for %s: %s — NORMAL", date_str, exc)
            regimes[date_str] = "NORMAL"
        logger.info("  %s: regime=%s", date_str, regimes[date_str])

    return regimes


# ── 5-min bars with Parquet cache + parallel fetch ───────────────────────────

def _cache_path(symbols: list[str], start: datetime, end: datetime, cache_dir: Path) -> Path:
    sym_hash = hashlib.sha256(",".join(sorted(symbols)).encode()).hexdigest()[:8]
    return cache_dir / f"bars_5min_{start.date()}_{end.date()}_{sym_hash}.parquet"


def _fetch_one_batch(
    client: StockHistoricalDataClient,
    symbols: list[str],
    start: datetime,
    end: datetime,
) -> dict[str, pd.DataFrame]:
    req = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        start=start,
        end=end,
        feed="iex",
    )
    bars   = client.get_stock_bars(req)
    df_all = bars.df
    result: dict[str, pd.DataFrame] = {}
    if df_all is None or df_all.empty:
        return result
    if isinstance(df_all.index, pd.MultiIndex):
        for sym in symbols:
            try:
                df = df_all.xs(sym, level="symbol")
                if not df.empty:
                    result[sym] = df
            except KeyError:
                pass
    elif symbols:
        result[symbols[0]] = df_all
    return result


def _fetch_5min_cold(
    client: StockHistoricalDataClient,
    symbols: list[str],
    start: datetime,
    end: datetime,
) -> dict[str, pd.DataFrame]:
    """Fetch all symbol batches × time chunks in parallel via ThreadPoolExecutor."""
    chunk_size_days = _FETCH_DAY_CHUNK * 7 // 5
    time_chunks: list[tuple[datetime, datetime]] = []
    cs = start
    while cs < end:
        ce = min(cs + timedelta(days=chunk_size_days), end)
        time_chunks.append((cs, ce))
        cs = ce

    sym_batches = [symbols[i:i + _FETCH_SYMBOL_BATCH] for i in range(0, len(symbols), _FETCH_SYMBOL_BATCH)]
    tasks = [(b, ts, te) for ts, te in time_chunks for b in sym_batches]

    logger.info(
        "  Fetching %d batches × %d time chunks = %d parallel tasks…",
        len(sym_batches), len(time_chunks), len(tasks),
    )

    result: dict[str, pd.DataFrame] = {}
    lock = threading.Lock()

    def _run(batch: list[str], t_start: datetime, t_end: datetime) -> dict[str, pd.DataFrame]:
        return _fetch_one_batch(client, batch, t_start, t_end)

    n_workers = min(8, len(tasks))
    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = {executor.submit(_run, *task): task for task in tasks}
        for future in as_completed(futures):
            batch, t_start, t_end = futures[future]
            try:
                chunk = future.result()
                with lock:
                    for sym, df in chunk.items():
                        if sym in result:
                            result[sym] = pd.concat([result[sym], df]).sort_index()
                            result[sym] = result[sym][~result[sym].index.duplicated(keep="last")]
                        else:
                            result[sym] = df
            except Exception as exc:
                logger.warning(
                    "  Fetch failed batch %s… %s→%s: %s — skipping",
                    batch[:3], t_start.date(), t_end.date(), exc,
                )

    return result


def fetch_5min_bars(
    symbols: list[str],
    start: datetime,
    end: datetime,
    cache_dir: Path | None = None,
) -> dict[str, pd.DataFrame]:
    """
    Fetch 5-min bars with Parquet disk cache.

    Cache key encodes start, end, and a hash of sorted symbols — stale data
    is impossible because the date range is embedded in the filename.
    Writes atomically via a .tmp file then rename.
    """
    cache_root = cache_dir or _CACHE_DIR
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_file = _cache_path(symbols, start, end, cache_root)

    if cache_file.exists():
        logger.info("  Cache hit: %s — loading from disk…", cache_file.name)
        df_all = pd.read_parquet(cache_file)
        result: dict[str, pd.DataFrame] = {}
        for sym in df_all.index.get_level_values("symbol").unique():
            result[sym] = df_all.xs(sym, level="symbol")
        logger.info("  Loaded %d symbols from cache.", len(result))
        return result

    logger.info(
        "  Cache miss — fetching from Alpaca: %d symbols, %s → %s",
        len(symbols), start.date(), end.date(),
    )
    client = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
    result = _fetch_5min_cold(client, symbols, start, end)
    logger.info("  Bars received for %d symbols.", len(result))

    if result:
        combined = pd.concat(
            {sym: df for sym, df in result.items()},
            names=["symbol"],
        )
        tmp = cache_file.with_suffix(".tmp")
        combined.to_parquet(tmp)
        tmp.rename(cache_file)
        logger.info("  Cached to %s", cache_file.name)

    return result
