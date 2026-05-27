import pytest
from unittest.mock import patch, MagicMock


class TestFredCacheExpansion:
    @pytest.mark.unit
    def test_load_fred_cache_returns_dict(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "FRED_CACHE_FILE", str(tmp_path / "fred.json"))
        from utils.io_utils import save_json
        save_json(str(tmp_path / "fred.json"), {"fred_modifier": 3})
        from macro.fred_cache import load_fred_cache
        result = load_fred_cache()
        assert isinstance(result, dict)
        assert result["fred_modifier"] == 3

    @pytest.mark.unit
    def test_cpi_yoy_computed_correctly(self):
        from macro.fred_cache import _compute_cpi_yoy
        # 13 obs: index 0 = most recent (280), index 12 = 12 months ago (260)
        cpi = [280.0] + [270.0] * 11 + [260.0]
        yoy = _compute_cpi_yoy(cpi)
        expected = (280.0 / 260.0 - 1) * 100
        assert abs(yoy - expected) < 0.01

    @pytest.mark.unit
    def test_modifier_clamped_to_range(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "FRED_MODIFIER_MIN", -15)
        monkeypatch.setattr(config, "FRED_MODIFIER_MAX", 15)
        from macro.fred_cache import _compute_modifier
        result = _compute_modifier(
            fed_direction="hiking",
            cpi_vs_target="above",
            yield_curve_inverted=True,
            credit_spread_risk_off=True,
            breakeven_high=True,
            unemployment_rising=True,
            claims_spike=True,
        )
        assert result == -15   # clamped to min

    @pytest.mark.unit
    def test_modifier_all_positive(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "FRED_MODIFIER_MIN", -15)
        monkeypatch.setattr(config, "FRED_MODIFIER_MAX", 15)
        from macro.fred_cache import _compute_modifier
        result = _compute_modifier(
            fed_direction="cutting",
            cpi_vs_target="below",
            yield_curve_inverted=False,
            credit_spread_risk_off=False,
            breakeven_high=False,
            unemployment_rising=False,
            claims_spike=False,
        )
        assert result == 7   # cutting(+5) + below_cpi(+2)

    @pytest.mark.unit
    def test_uses_session_not_bare_get(self):
        import macro.fred_cache as fc
        import inspect
        src = inspect.getsource(fc)
        assert "requests.get(" not in src, "Must use Session, not bare requests.get()"


class TestYieldCurveOverride:
    @pytest.mark.unit
    def test_bull_trend_overridden_to_defensive_when_inverted(self):
        from macro.regime import _apply_fred_overrides
        result = _apply_fred_overrides("BULL_TREND", {"yield_curve_inverted": True})
        assert result == "DEFENSIVE"

    @pytest.mark.unit
    def test_normal_not_overridden_without_inversion(self):
        from macro.regime import _apply_fred_overrides
        result = _apply_fred_overrides("NORMAL", {"yield_curve_inverted": False})
        assert result == "NORMAL"

    @pytest.mark.unit
    def test_crisis_not_overridden(self):
        from macro.regime import _apply_fred_overrides
        result = _apply_fred_overrides("CRISIS", {"yield_curve_inverted": True})
        assert result == "CRISIS"

    @pytest.mark.unit
    def test_opportunity_overridden_when_inverted(self):
        from macro.regime import _apply_fred_overrides
        result = _apply_fred_overrides("OPPORTUNITY", {"yield_curve_inverted": True})
        assert result == "DEFENSIVE"

    @pytest.mark.unit
    def test_normal_overridden_to_defensive_when_inverted(self):
        from macro.regime import _apply_fred_overrides
        result = _apply_fred_overrides("NORMAL", {"yield_curve_inverted": True})
        assert result == "DEFENSIVE"

    @pytest.mark.unit
    def test_crash_recovery_not_overridden_when_inverted(self):
        from macro.regime import _apply_fred_overrides
        result = _apply_fred_overrides("CRASH_RECOVERY", {"yield_curve_inverted": True})
        assert result == "CRASH_RECOVERY"


class TestCatalystSurpriseScore:
    @pytest.mark.unit
    def test_hot_cpi_is_bearish(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "FRED_CACHE_FILE", str(tmp_path / "fred.json"))
        monkeypatch.setattr(config, "FRED_CPI_TARGET_PCT", 2.0)
        from utils.io_utils import save_json
        save_json(str(tmp_path / "fred.json"), {"cpi_yoy_pct": 5.5, "cpi_vs_target": "above"})
        from macro.economic_calendar import get_catalyst_surprise_score
        score = get_catalyst_surprise_score("CPI")
        assert score < 0

    @pytest.mark.unit
    def test_low_cpi_is_bullish(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "FRED_CACHE_FILE", str(tmp_path / "fred.json"))
        monkeypatch.setattr(config, "FRED_CPI_TARGET_PCT", 2.0)
        from utils.io_utils import save_json
        save_json(str(tmp_path / "fred.json"), {"cpi_yoy_pct": 1.0, "cpi_vs_target": "below"})
        from macro.economic_calendar import get_catalyst_surprise_score
        score = get_catalyst_surprise_score("CPI")
        assert score > 0

    @pytest.mark.unit
    def test_unknown_event_returns_zero(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "FRED_CACHE_FILE", str(tmp_path / "fred.json"))
        from utils.io_utils import save_json
        save_json(str(tmp_path / "fred.json"), {})
        from macro.economic_calendar import get_catalyst_surprise_score
        assert get_catalyst_surprise_score("EARNINGS") == 0.0


class TestSectorFredScoring:
    @pytest.mark.unit
    def test_xlf_hiking_tailwind(self):
        from utils.ticker_quality import _score_sector_fred
        pts, gap = _score_sector_fred("XLF", {"fed_direction": "hiking"})
        assert pts == 3

    @pytest.mark.unit
    def test_xlu_hiking_headwind_clamped_to_zero(self):
        from utils.ticker_quality import _score_sector_fred
        pts, gap = _score_sector_fred("XLU", {"fed_direction": "hiking"})
        assert pts == 0   # negative clamped to 0
        assert gap is not None  # gap reported

    @pytest.mark.unit
    def test_xlk_cutting_tailwind(self):
        from utils.ticker_quality import _score_sector_fred
        pts, gap = _score_sector_fred("XLK", {"fed_direction": "cutting"})
        assert pts == 3

    @pytest.mark.unit
    def test_unknown_etf_returns_zero(self):
        from utils.ticker_quality import _score_sector_fred
        pts, gap = _score_sector_fred("XYZ", {"fed_direction": "hiking"})
        assert pts == 0
