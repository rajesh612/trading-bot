#!/usr/bin/env python3
"""Backtest CLI entry point. All logic lives in the backtest/ package."""
import argparse
import logging
from datetime import datetime, timedelta

from backtest.runner import run_backtest
from utils.constants import ET

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Run backtest over a date range.")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--days", type=int, default=30, help="trailing N trading days (default 30)")
    group.add_argument("--start", type=str, help="start date YYYY-MM-DD (use with --end)")
    ap.add_argument("--end", type=str, help="end date YYYY-MM-DD (default: today)")
    return ap.parse_args()


def _resolve_date_range(args: argparse.Namespace) -> tuple[datetime, datetime]:
    today = datetime.now(tz=ET).replace(hour=0, minute=0, second=0, microsecond=0)
    if args.start:
        start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=ET)
        end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=ET) if args.end else today
    else:
        # --days: approximate by going back days*1.5 calendar days, runner filters non-trading days
        end = today
        start = today - timedelta(days=int(args.days * 1.5))
    return start, end


if __name__ == "__main__":
    args = _parse_args()
    start, end = _resolve_date_range(args)
    run_backtest(start, end)
