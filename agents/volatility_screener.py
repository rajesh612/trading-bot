import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

import config
from broker.alpaca_client import AlpacaClient
from scheduler_jobs import _schedule_retry
from utils.data_health import _tracker
from utils.http import yahoo_get

logger = logging.getLogger(__name__)

_MARKET_CAP_URL = (
    "https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
    "?modules=price"
)


def _fetch_market_cap(symbol: str) -> float | None:
    """Return market cap in dollars from Yahoo Finance, or None if unavailable."""
    try:
        data = yahoo_get(_MARKET_CAP_URL.format(symbol=symbol))
        result = data.get("quoteSummary", {}).get("result") or []
        if not result:
            return None
        raw = result[0].get("price", {}).get("marketCap", {}).get("raw")
        return float(raw) if raw else None
    except Exception:
        return None


def _score_ticker(symbol: str, client: AlpacaClient) -> float:
    try:
        df = client.get_daily_bars(symbol, days=30)
        if df is None or len(df) < 5:
            return 0.0

        close = df["close"]
        prev_close = close.shift(1)

        # ATR(14) as % of price
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr14 = tr.ewm(com=13, min_periods=14).mean().iloc[-1]
        atr_pct = (atr14 / close.iloc[-1]) * 100

        # Average intraday range % (last 5 days)
        range_pct = ((df["high"] - df["low"]) / close).tail(5).mean() * 100

        # Volume ratio: recent 5-day avg vs 30-day avg
        avg_vol_30 = df["volume"].mean()
        avg_vol_5 = df["volume"].tail(5).mean()
        vol_ratio = (avg_vol_5 / avg_vol_30) if avg_vol_30 > 0 else 1.0

        # Pre-market gap approximation: open vs prior close (last 5 days)
        gap_pct = ((df["open"] - close.shift(1)).abs() / close.shift(1)).tail(5).mean() * 100

        # Weighted composite volatility score
        score = (
            atr_pct    * 0.35 +
            range_pct  * 0.25 +
            vol_ratio  * 20   * 0.25 +   # scale ratio to roughly same magnitude
            gap_pct    * 0.15
        )
        return float(score)
    except Exception as exc:
        logger.warning(f"Volatility score failed for {symbol}: {exc}")
        return 0.0


def run_volatility_screener() -> list[str]:
    import sectors
    tickers = sectors.load_universe()

    # Pre-filter: exclude symbols below market cap threshold
    if config.MIN_MARKET_CAP_B > 0:
        min_cap = config.MIN_MARKET_CAP_B * 1e9

        # Fetch market caps concurrently
        cap_results: dict[str, float | None] = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            future_to_sym = {pool.submit(_fetch_market_cap, t): t for t in tickers}
            for fut in as_completed(future_to_sym):
                sym = future_to_sym[fut]
                try:
                    cap_results[sym] = fut.result()
                except Exception:
                    cap_results[sym] = None

        # Detect API-level failure before filtering
        none_count = sum(1 for v in cap_results.values() if v is None)
        failure_rate = none_count / max(len(tickers), 1)

        if failure_rate >= config.DATA_SCREENER_FAILURE_RATE_THRESHOLD:
            logger.error(
                "Market cap API failure: %d/%d fetches returned None (%.0f%%) — "
                "skipping filter, using full universe",
                none_count, len(tickers), failure_rate * 100,
            )
            _schedule_retry("screener", run_volatility_screener)
            eligible = tickers  # bypass filter
        else:
            eligible = []
            for ticker in tickers:  # preserve original order
                cap = cap_results.get(ticker)
                if cap is not None and cap >= min_cap:
                    eligible.append(ticker)
                elif cap is not None:
                    logger.debug(f"SCREEN OUT {ticker}: market cap ${cap:,.0f} < ${min_cap:,.0f}")
                else:
                    logger.debug(f"SCREEN OUT {ticker}: market cap unavailable")

        logger.info(
            "Market cap filter (%sB): %d → %d tickers%s",
            config.MIN_MARKET_CAP_B, len(tickers), len(eligible),
            " [API FAILURE — filter bypassed]"
            if failure_rate >= config.DATA_SCREENER_FAILURE_RATE_THRESHOLD else "",
        )
        tickers = eligible

    if len(tickers) <= config.MAX_TICKERS_DIRECT:
        logger.info(f"{len(tickers)} tickers ≤ {config.MAX_TICKERS_DIRECT} — no screening needed.")
        config.DAILY_TRADEABLE_FILE.write_text("\n".join(tickers))
        _tracker.record_success("screener")
        return tickers

    logger.info(f"Screening {len(tickers)} tickers for top 50 by volatility…")
    client = AlpacaClient()

    scores: dict[str, float] = {}
    for ticker in tickers:
        scores[ticker] = _score_ticker(ticker, client)
        logger.debug(f"  {ticker}: {scores[ticker]:.2f}")

    top50 = sorted(scores, key=lambda s: scores[s], reverse=True)[:50]
    config.DAILY_TRADEABLE_FILE.write_text("\n".join(top50))
    logger.info(f"Volatility screen done. Top 50: {top50}")
    _tracker.record_success("screener")
    return top50


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO)
    run_volatility_screener()
