import json
import logging
import re

import pandas as pd
import anthropic

import api_budget
import config
from alpaca_client import AlpacaClient

logger = logging.getLogger(__name__)


def _load_universe() -> list[str]:
    src = config.DAILY_TRADEABLE_FILE if config.DAILY_TRADEABLE_FILE.exists() else config.TICKERS_FILE
    return [t.strip().upper() for t in src.read_text().splitlines() if t.strip()]


def _score_stock(symbol: str, client: AlpacaClient) -> dict | None:
    try:
        df = client.get_daily_bars(symbol, days=30)
        if df is None or len(df) < 5:
            return None

        close = df["close"]
        prev_close = close.shift(1)

        avg_vol_20 = df["volume"].tail(20).mean()
        vol_spike = float(df["volume"].iloc[-1] / avg_vol_20) if avg_vol_20 > 0 else 1.0

        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr14 = tr.ewm(com=13, min_periods=14).mean().iloc[-1]
        atr_pct = float(atr14 / close.iloc[-1] * 100)

        gap_pct = float(
            (df["open"].iloc[-1] - close.iloc[-2]) / close.iloc[-2] * 100
        ) if len(df) >= 2 else 0.0

        return {
            "symbol": symbol,
            "price": round(float(close.iloc[-1]), 2),
            "vol_spike": round(vol_spike, 2),
            "atr_pct": round(atr_pct, 2),
            "gap_pct": round(gap_pct, 2),
        }
    except Exception as exc:
        logger.warning(f"Scoring failed for {symbol}: {exc}")
        return None


def run_watch_list_agent() -> list[str]:
    universe = _load_universe()
    logger.info(f"Scoring {len(universe)} stocks for today's watch list…")

    client = AlpacaClient()
    scored = [s for sym in universe if (s := _score_stock(sym, client))]

    if not scored:
        logger.error("No stocks scored — watch list empty.")
        return []

    strategy_memory = (
        config.STRATEGY_MEMORY_FILE.read_text()
        if config.STRATEGY_MEMORY_FILE.exists()
        else ""
    )

    ai = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    response = ai.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": (
                    "You are an expert day trader selecting the best stocks to trade today.\n\n"
                    "Select 10–15 stocks from the list provided. Prefer:\n"
                    "- vol_spike > 1.5 (strong relative volume)\n"
                    "- atr_pct > 1.5% (meaningful intraday range)\n"
                    "- Notable gap % (catalyst signal)\n\n"
                    "Return ONLY a JSON array of ticker symbols. No explanation.\n"
                    "Example: [\"AAPL\", \"TSLA\", \"NVDA\"]"
                ),
                "cache_control": {"type": "ephemeral"},
            },
            *(
                [
                    {
                        "type": "text",
                        "text": f"Strategy memory from prior sessions:\n{strategy_memory}",
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
                if strategy_memory
                else []
            ),
        ],
        messages=[
            {
                "role": "user",
                "content": f"Stock metrics:\n{json.dumps(scored, indent=2)}\n\nPick the best 10–15 for today.",
            }
        ],
    )

    try:
        usage = response.usage
        api_budget.record_usage(
            input_tokens=getattr(usage, "input_tokens", 0),
            output_tokens=getattr(usage, "output_tokens", 0),
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0),
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0),
        )
    except Exception:
        pass

    text = response.content[0].text.strip()
    match = re.search(r"\[.*?\]", text, re.DOTALL)
    if match:
        watch_list = [s.upper() for s in json.loads(match.group()) if isinstance(s, str)]
    else:
        watch_list = [
            s["symbol"]
            for s in sorted(scored, key=lambda x: x["vol_spike"] * x["atr_pct"], reverse=True)[:15]
        ]

    watch_list = watch_list[: config.WATCH_LIST_MAX]
    config.WATCH_LIST_FILE.write_text("\n".join(watch_list))
    logger.info(f"Watch list ({len(watch_list)} stocks): {watch_list}")
    return watch_list


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO)
    run_watch_list_agent()
