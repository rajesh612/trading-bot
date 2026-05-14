"""
Earnings calendar and PEAD detection unit tests.
Run with: pytest tests/test_earnings.py -v -m unit
No API calls — all tests use synthetic fixtures.
"""
import json
import os
from datetime import datetime

import pytest

from utils.constants import ET

@pytest.mark.unit
class TestEarningsCalendar:
    # ── Shared helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _perfect_block() -> dict:
        """Return a recently_reported block where all 5 PEAD conditions pass."""
        from datetime import timedelta
        report_date = (datetime.now(ET) - timedelta(days=2)).strftime("%Y-%m-%d")
        return {
            "report_date":                 report_date,
            "eps_actual":                  1.10,
            "eps_estimate":                1.00,
            "eps_surprise_pct":            10.0,   # cond 1 ✓ (≥ 5%)
            "revenue_beat":                True,   # cond 2 ✓
            "guidance_change":             "raised",  # cond 3 ✓
            "earnings_day_return_pct":     5.0,    # cond 4 ✓ (≥ 3%, closed high)
            "earnings_day_close_position": 0.85,
            "earnings_day_volume_ratio":   3.0,    # cond 5 ✓ (≥ 2×)
        }

    def _write_cache(self, tmp_path, symbol: str = "NVDA", **block_overrides) -> None:
        """Write an earnings cache for today with a single-symbol recently_reported block.

        Pass keyword args to selectively override fields in the perfect block, e.g.:
            self._write_cache(tmp_path, guidance_change="lowered")
        """
        import config
        config.EARNINGS_CALENDAR_FILE = tmp_path / "earnings_calendar.json"
        block = {**self._perfect_block(), **block_overrides}
        payload = {
            "date": datetime.now(ET).strftime("%Y-%m-%d"),
            "symbols": {symbol: {"next_earnings_date": None, "recently_reported": block}},
        }
        with open(config.EARNINGS_CALENDAR_FILE, "w") as f:
            json.dump(payload, f)

    # ── Imminent flag ─────────────────────────────────────────────────────────

    def test_is_earnings_imminent_no_cache(self, tmp_path):
        import earnings_calendar as ec
        import config
        config.EARNINGS_CALENDAR_FILE = tmp_path / "earnings_calendar.json"
        assert ec.is_earnings_imminent("NVDA") is False  # no file → conservative False

    def test_is_earnings_imminent_stale_file(self, tmp_path):
        import earnings_calendar as ec
        import config
        config.EARNINGS_CALENDAR_FILE = tmp_path / "earnings_calendar.json"
        stale = {"date": "2020-01-01", "symbols": {"NVDA": {"next_earnings_date": "2020-01-02"}}}
        with open(config.EARNINGS_CALENDAR_FILE, "w") as f:
            json.dump(stale, f)
        assert ec.is_earnings_imminent("NVDA") is False  # stale date → conservative False

    # ── PEAD detection ────────────────────────────────────────────────────────

    def test_detect_pead_no_cache(self, tmp_path):
        import earnings_calendar as ec
        import config
        config.EARNINGS_CALENDAR_FILE = tmp_path / "earnings_calendar.json"
        qualifies, score = ec.detect_pead_setup("NVDA")
        assert qualifies is False
        assert score == 0

    def test_detect_pead_all_five_conditions(self, tmp_path):
        import earnings_calendar as ec
        self._write_cache(tmp_path)  # perfect block — all 5 conditions pass
        qualifies, score = ec.detect_pead_setup("NVDA")
        assert qualifies is True
        # eps=10.0 hits PEAD_EPS_BEAT_SOLID → 25 pts; other 4 conds × 20 = 80; total = 105
        assert score == 105

    def test_detect_pead_three_conditions_fails(self, tmp_path):
        import earnings_calendar as ec
        self._write_cache(
            tmp_path,
            earnings_day_return_pct=1.0,    # cond 4 ✗ (< 3%)
            earnings_day_close_position=0.5,
            earnings_day_volume_ratio=1.0,  # cond 5 ✗ (< 2×)
        )
        qualifies, score = ec.detect_pead_setup("NVDA")
        assert qualifies is False
        # eps=10% → 25 (PEAD_EPS_BEAT_SOLID) + revenue=20 + guidance=20 = 65
        assert score == 65

    def test_detect_pead_guidance_lowered_reduces_score(self, tmp_path):
        """Lowered guidance fails cond 3 (−20 pts). Other 4 pass → score=85 > threshold → qualifies."""
        import earnings_calendar as ec
        self._write_cache(tmp_path, guidance_change="lowered")  # cond 3 ✗
        qualifies, score = ec.detect_pead_setup("NVDA")
        # eps=10% → 25 + revenue=20 + earn_day=20 + vol=20 = 85
        assert score == 85
        assert qualifies is True  # 4/5 conditions still meet the 80pt threshold

    def test_detect_pead_guidance_lowered_plus_weak_reaction_fails(self, tmp_path):
        """Lowered guidance + poor earnings-day reaction → cond 1(25) + cond 2(20) + cond 5(20) = 65."""
        import earnings_calendar as ec
        self._write_cache(
            tmp_path,
            guidance_change="lowered",       # cond 3 ✗
            earnings_day_return_pct=1.0,     # cond 4 ✗ (< 3%)
            earnings_day_close_position=0.5,
        )
        qualifies, score = ec.detect_pead_setup("NVDA")
        assert qualifies is False
        # eps=10% → 25 + revenue=20 + vol=20 = 65 (guidance and reaction both fail)
        assert score == 65

    def test_detect_pead_unknown_guidance_allowed(self, tmp_path):
        import earnings_calendar as ec
        self._write_cache(tmp_path, guidance_change="unknown")  # cond 3 ✓ (unknown ≠ lowered)
        qualifies, score = ec.detect_pead_setup("NVDA")
        assert qualifies is True
        assert score == 105  # eps=10% → 25; 4 others × 20 = 80; total = 105


# ── Plan 3: Mode Routing & Learning ──────────────────────────────────────────


@pytest.mark.unit
class TestEarningsCalendarCacheHelpers:
    def _write_cache(self, tmp_path, symbol="NVDA", days_until=3, reported_block=None):
        import config, json
        from datetime import datetime, timedelta
        from utils.constants import ET
        config.EARNINGS_CALENDAR_FILE = tmp_path / "earnings_calendar.json"
        today_et = datetime.now(ET).date()
        next_date = (today_et + timedelta(days=days_until)).isoformat()
        entry = {"next_earnings_date": next_date}
        if reported_block:
            entry["recently_reported"] = reported_block
        payload = {
            "date": today_et.isoformat(),
            "symbols": {symbol: entry},
        }
        config.EARNINGS_CALENDAR_FILE.write_text(json.dumps(payload))

    def test_days_until_earnings_returns_correct_count(self, tmp_path):
        self._write_cache(tmp_path, days_until=5)
        from earnings_calendar import days_until_earnings
        d = days_until_earnings("NVDA")
        assert d == 5

    def test_days_until_earnings_returns_none_for_unknown(self, tmp_path):
        import config
        config.EARNINGS_CALENDAR_FILE = tmp_path / "earnings_calendar.json"
        from earnings_calendar import days_until_earnings
        assert days_until_earnings("UNKNOWN") is None

    def test_is_earnings_imminent_true_when_in_1_day(self, tmp_path):
        self._write_cache(tmp_path, days_until=1)
        from earnings_calendar import is_earnings_imminent
        assert is_earnings_imminent("NVDA", days=1) is True

    def test_is_earnings_imminent_false_when_far(self, tmp_path):
        self._write_cache(tmp_path, days_until=10)
        from earnings_calendar import is_earnings_imminent
        assert is_earnings_imminent("NVDA", days=1) is False

    def test_recently_reported_returns_block_when_fresh(self, tmp_path):
        from datetime import date, timedelta
        report_date = (date.today() - timedelta(days=2)).isoformat()
        block = {"report_date": report_date, "eps_surprise_pct": 10.0}
        self._write_cache(tmp_path, reported_block=block)
        from earnings_calendar import recently_reported
        result = recently_reported("NVDA", days=5)
        assert result is not None
        assert result["eps_surprise_pct"] == 10.0

    def test_recently_reported_returns_none_when_stale(self, tmp_path):
        from datetime import date, timedelta
        old_date = (date.today() - timedelta(days=10)).isoformat()
        block = {"report_date": old_date, "eps_surprise_pct": 10.0}
        self._write_cache(tmp_path, reported_block=block)
        from earnings_calendar import recently_reported
        assert recently_reported("NVDA", days=5) is None


# ── trading_agent: persistence helpers ───────────────────────────────────────


@pytest.mark.unit
class TestPeadDetection:
    """Tests for detect_pead_setup — pure logic, no network calls."""

    def _perfect_block(self) -> dict:
        """Return a recently_reported block that satisfies all 5 PEAD conditions.

        Scoring with graded EPS (eps=10% hits PEAD_EPS_BEAT_SOLID → 25 pts):
        25 (EPS) + 20 (rev) + 20 (guidance) + 20 (reaction) + 20 (vol) = 105
        """
        # Use today's date so the block passes the recency gate in recently_reported()
        today = datetime.now(ET).strftime("%Y-%m-%d")
        return {
            "report_date":                today,
            "eps_surprise_pct":           10.0,   # == PEAD_EPS_BEAT_SOLID → 25 pts
            "revenue_beat":               True,
            "guidance_change":            "raised",
            "earnings_day_return_pct":    5.0,    # > PEAD_DAY_RETURN_MIN (3%)
            "earnings_day_close_position": 0.85,  # > PEAD_CLOSE_POSITION_MIN (0.7)
            "earnings_day_volume_ratio":  3.0,    # > PEAD_VOLUME_RATIO_MIN (2.0)
        }

    def _write_cache(self, tmp_path, symbol: str, block: dict) -> None:
        import config
        today = datetime.now(ET).strftime("%Y-%m-%d")
        payload = {
            "date": today,
            "symbols": {symbol: {"next_earnings_date": None, "recently_reported": block}},
        }
        cache_file = tmp_path / "earnings_calendar.json"
        cache_file.write_text(json.dumps(payload))
        # Patch config to point at tmp file
        config.EARNINGS_CALENDAR_FILE = cache_file

    def test_all_conditions_met_qualifies(self, tmp_path):
        import config
        original = config.EARNINGS_CALENDAR_FILE
        try:
            self._write_cache(tmp_path, "AAPL", self._perfect_block())
            from earnings_calendar import detect_pead_setup
            qualifies, score = detect_pead_setup("AAPL")
            assert qualifies is True
            assert score == 105  # 25(eps=10%) + 20×4
        finally:
            config.EARNINGS_CALENDAR_FILE = original

    def test_eps_miss_reduces_score(self, tmp_path):
        import config
        original = config.EARNINGS_CALENDAR_FILE
        try:
            block = self._perfect_block()
            block["eps_surprise_pct"] = 1.0  # marginal hit ≥1% → 10 pts
            self._write_cache(tmp_path, "MSFT", block)
            from earnings_calendar import detect_pead_setup
            qualifies, score = detect_pead_setup("MSFT")
            assert score == 90   # 10(eps marginal) + 20×4 — still qualifies
            assert qualifies is True
        finally:
            config.EARNINGS_CALENDAR_FILE = original

    def test_two_conditions_missing_does_not_qualify(self, tmp_path):
        import config
        original = config.EARNINGS_CALENDAR_FILE
        try:
            block = self._perfect_block()
            block["eps_surprise_pct"] = 1.0         # marginal → 10 pts
            block["revenue_beat"] = False            # miss cond 2 → 0 pts
            self._write_cache(tmp_path, "NVDA", block)
            from earnings_calendar import detect_pead_setup
            qualifies, score = detect_pead_setup("NVDA")
            assert qualifies is False
            assert score == 70   # 10 + 0 + 20×3 = 70 < threshold 80
        finally:
            config.EARNINGS_CALENDAR_FILE = original

    def test_guidance_lowered_disqualifies_condition(self, tmp_path):
        import config
        original = config.EARNINGS_CALENDAR_FILE
        try:
            block = self._perfect_block()
            block["guidance_change"] = "lowered"     # miss cond 3 → 0 pts
            block["revenue_beat"] = False            # miss cond 2 → 0 pts
            self._write_cache(tmp_path, "TSLA", block)
            from earnings_calendar import detect_pead_setup
            qualifies, score = detect_pead_setup("TSLA")
            assert qualifies is False
            assert score == 65   # 25(eps=10%) + 0 + 0 + 20×2 = 65 < threshold 80
        finally:
            config.EARNINGS_CALENDAR_FILE = original

    def test_no_recently_reported_returns_false_zero(self, tmp_path):
        import config
        original = config.EARNINGS_CALENDAR_FILE
        try:
            today = datetime.now(ET).strftime("%Y-%m-%d")
            payload = {"date": today, "symbols": {"AMZN": {"next_earnings_date": None}}}
            cache_file = tmp_path / "earnings_calendar.json"
            cache_file.write_text(json.dumps(payload))
            config.EARNINGS_CALENDAR_FILE = cache_file
            from earnings_calendar import detect_pead_setup
            qualifies, score = detect_pead_setup("AMZN")
            assert qualifies is False
            assert score == 0
        finally:
            config.EARNINGS_CALENDAR_FILE = original


# ── economic_calendar: hardcoded events + risk window ────────────────────────
