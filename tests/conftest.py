import pytest


@pytest.fixture(autouse=True)
def _redirect_cycle_telemetry(monkeypatch, tmp_path):
    """Prevent tests from writing to the production cycle_telemetry.jsonl."""
    import config
    monkeypatch.setattr(config, "CYCLE_TELEMETRY_FILE", tmp_path / "cycle_telemetry.jsonl")
