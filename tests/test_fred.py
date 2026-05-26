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
