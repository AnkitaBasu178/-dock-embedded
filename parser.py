"""Parse serial lines from the ESP32s.

Line format (one measurement per line, checksum = XOR of every byte before '*'):

    VITAL type=spo2 value=98 ir=150000*5A
    VITAL type=weight value=68.42*3C

Anything that is not a perfectly valid VITAL line is dropped and logged. A bad line
never becomes a reading.
"""
import logging
import math
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("dock.parser")

SERIAL_TYPES = {"heart_rate", "spo2", "temperature", "weight", "height", "gsr"}

# Physically impossible values are treated as corrupt data (e.g. the MAX30102
# algorithm's -999 "invalid" marker). This is glitch filtering, not clinical judgement.
BOUNDS = {
    "heart_rate": (20.0, 250.0),
    "spo2": (1.0, 100.0),
    "temperature": (10.0, 50.0),
    "weight": (0.0, 500.0),
    "height": (30.0, 260.0),
    "gsr": (1.0, 3400.0),
}


@dataclass
class Sample:
    type: str
    value: float
    extras: dict[str, float] = field(default_factory=dict)


def checksum(body: str) -> int:
    cs = 0
    for b in body.encode("utf-8", "replace"):
        cs ^= b
    return cs


def parse_line(raw: str) -> Optional[Sample]:
    line = raw.strip()
    if not line:
        return None
    if not line.startswith("VITAL"):
        if "VITAL" in line:
            logger.warning("dropped line with garbage before VITAL: %r", line[:80])
        else:
            logger.debug("ignored non-data line: %r", line[:80])  # boot messages etc.
        return None

    body, star, cs = line.rpartition("*")
    if not star or len(cs) != 2:
        logger.warning("dropped truncated line (no checksum): %r", line[:80])
        return None
    try:
        expected = int(cs, 16)
    except ValueError:
        logger.warning("dropped line with bad checksum field: %r", line[:80])
        return None
    if checksum(body) != expected:
        logger.warning("dropped line, checksum mismatch: %r", line[:80])
        return None

    tokens = body.split()
    if not tokens or tokens[0] != "VITAL":
        logger.warning("dropped malformed line: %r", line[:80])
        return None

    fields: dict[str, str] = {}
    for tok in tokens[1:]:
        key, eq, val = tok.partition("=")
        if not eq or not key:
            logger.warning("dropped line, bad field %r: %r", tok, line[:80])
            return None
        fields[key] = val

    vtype = fields.pop("type", None)
    value_s = fields.pop("value", None)
    if vtype not in SERIAL_TYPES or value_s is None:
        logger.warning("dropped line, unknown type or missing value: %r", line[:80])
        return None

    try:
        value = float(value_s)
        extras = {k: float(v) for k, v in fields.items()}
    except ValueError:
        logger.warning("dropped line, non-numeric field: %r", line[:80])
        return None
    if not math.isfinite(value) or not all(math.isfinite(v) for v in extras.values()):
        logger.warning("dropped line, non-finite number: %r", line[:80])
        return None

    lo, hi = BOUNDS[vtype]
    if not (lo <= value <= hi):
        logger.warning("dropped line, %s=%s is physically impossible: %r", vtype, value, line[:80])
        return None

    return Sample(type=vtype, value=value, extras=extras)


def parse_ecg_line(raw: str) -> Optional[list[int]]:
    """`ECG d=512,515,...*HH` -> list of raw 12-bit ADC values, or None if not a perfect line."""
    line = raw.strip()
    if not line.startswith("ECG "):
        return None
    body, star, cs = line.rpartition("*")
    if not star or len(cs) != 2:
        return None
    try:
        if checksum(body) != int(cs, 16):
            return None
    except ValueError:
        return None
    key, eq, data = body[4:].strip().partition("=")
    if key != "d" or not eq:
        return None
    try:
        vals = [int(x) for x in data.split(",")]
    except ValueError:
        return None
    if not vals or any(v < 0 or v > 4095 for v in vals):
        return None
    return vals


def parse_status_line(raw: str) -> Optional[dict]:
    """`STATUS max30102=1 finger=0 ecg=1 gsr=0*HH` -> {"max30102": 1.0, ...} or None."""
    line = raw.strip()
    if not line.startswith("STATUS "):
        return None
    body, star, cs = line.rpartition("*")
    if not star or len(cs) != 2:
        return None
    try:
        if checksum(body) != int(cs, 16):
            return None
    except ValueError:
        return None
    out = {}
    for tok in body.split()[1:]:
        k, eq, v = tok.partition("=")
        if not eq or v not in ("0", "1"):
            return None
        out[k] = float(v)
    return out or None
