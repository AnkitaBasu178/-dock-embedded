"""Environment-driven configuration. Read at call time so tests can change it."""
import os
from typing import Optional


def simulate() -> bool:
    return os.environ.get("SIMULATE", "").strip().lower() in ("1", "true", "yes", "on")


def port() -> int:
    return int(os.environ.get("PORT", "8010"))


def device_key() -> Optional[str]:
    return os.environ.get("DOCK_DEVICE_KEY") or None


def serial_ports() -> str:
    """'auto' (scan for USB-serial devices) or a comma-separated list of ports."""
    return os.environ.get("SERIAL_PORTS", "auto")


def serial_baud() -> int:
    return int(os.environ.get("SERIAL_BAUD", "115200"))


def hidden_types() -> set:
    """Comma-separated types to keep OUT of /vitals, e.g. HIDE_TYPES=gsr until P4
    has added that type. Hidden readings are still collected, just not served."""
    return {t.strip() for t in os.environ.get("HIDE_TYPES", "").split(",") if t.strip()}


def sim_speed() -> float:
    """1.0 = real-time simulation (~45 s). Larger is faster (used by tests)."""
    return float(os.environ.get("SIM_SPEED", "1"))


def confidence_floor() -> float:
    """DEMO ONLY. If set (e.g. 0.89) every reported confidence is raised to at least this.
    The real value is still computed; this just lifts it. Unset = real confidence."""
    raw = os.environ.get("CONFIDENCE_FLOOR", "").strip()
    try:
        return min(0.98, max(0.0, float(raw))) if raw else 0.0
    except ValueError:
        return 0.0


def continuous() -> bool:
    """true = never stop after a good reading: /vitals always shows the newest reading of each type."""
    return os.environ.get("CONTINUOUS", "").strip().lower() in ("1", "true", "yes", "on")


def relay() -> bool:
    """true = cloud copy (Render): no serial/simulator, data arrives via POST /ingest."""
    return os.environ.get("RELAY", "").strip().lower() in ("1", "true", "yes", "on")


def push_url() -> str:
    """Set on the Mac to the Render URL: the Mac then pushes its readings there."""
    return os.environ.get("PUSH_URL", "").strip().rstrip("/")


def ingest_key() -> Optional[str]:
    return os.environ.get("INGEST_KEY") or None


def fresh_seconds() -> float:
    """/data only shows a reading this recent (sensor stopped reading = it disappears)."""
    try:
        return float(os.environ.get("FRESH_SECONDS", "15"))
    except ValueError:
        return 15.0


def autostart() -> bool:
    """true = measuring from boot (old behaviour). Default: wait for /sensors/start."""
    return os.environ.get("AUTOSTART", "").strip().lower() in ("1", "true", "yes", "on")
