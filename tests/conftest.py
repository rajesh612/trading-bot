import pytest


@pytest.fixture(autouse=True)
def _redirect_cycle_telemetry(monkeypatch, tmp_path):
    """Prevent tests from writing to the production cycle_telemetry.jsonl."""
    import config
    monkeypatch.setattr(config, "CYCLE_TELEMETRY_FILE", tmp_path / "cycle_telemetry.jsonl")


@pytest.fixture(autouse=True)
def _block_stale_research_telegram(monkeypatch, tmp_path):
    """Prevent notify_stale_research from sending real Telegram alerts in tests.

    Two guards:
    1. Redirect the disk sentinel to tmp_path so writes don't pollute production state.
    2. Patch notify_stale_research itself to a no-op — tests that want to assert on
       staleness alerts should explicitly un-patch or capture the call themselves.
    """
    import config
    import notifications.telegram as tg
    monkeypatch.setattr(config, "STALE_RESEARCH_ALERTED_FILE", tmp_path / "stale_research_alerted.json")
    monkeypatch.setattr(tg, "notify_stale_research", lambda age_min: None)
