import logging

import numpy as np
import pandas as pd

import config
from alpaca_client import AlpacaClient

logger = logging.getLogger(__name__)


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
    raw = config.TICKERS_FILE.read_text().strip().splitlines()
    tickers = [t.strip().upper() for t in raw if t.strip()]

    if len(tickers) <= config.MAX_TICKERS_DIRECT:
        logger.info(f"{len(tickers)} tickers ≤ 50 — no screening needed.")
        config.DAILY_TRADEABLE_FILE.write_text("\n".join(tickers))
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
    return top50


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO)
    run_volatility_screener()
