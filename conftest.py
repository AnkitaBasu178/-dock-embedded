import pytest


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("DOCK_DEVICE_KEY", raising=False)
    monkeypatch.delenv("SIMULATE", raising=False)
    monkeypatch.setenv("AUTOSTART", "true")
    for k in ("RELAY", "PUSH_URL", "INGEST_KEY", "CONFIDENCE_FLOOR", "CONTINUOUS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SIM_SPEED", "20")  # a 45 s simulated run takes ~2 s


def make_line(body: str) -> str:
    """Build a valid serial line (independent of app.parser's own helper)."""
    cs = 0
    for b in body.encode():
        cs ^= b
    return f"{body}*{cs:02X}"
