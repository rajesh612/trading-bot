import config
from indicators.values import IndicatorValues


def passes_vwap_guard(iv: IndicatorValues) -> bool:
    """False if price exceeds the upper 2-sigma VWAP band (overextension hard block)."""
    if not config.VWAP_OVEREXTENSION_GATE:
        return True
    return iv.current_price <= iv.vwap_std2_upper
