"""Turns a stream of serial samples into readings with attempts and retakes.

For each vital type: collect a window of samples -> one reading (median value,
confidence from confidence.py). If confidence < 0.60 the next window is an
automatic retake (the earlier attempt is kept, as the contract requires), up to
3 attempts. After a good reading, or the 3rd attempt, that type stops until
POST /measure/start re-arms the run.
"""
import threading
from datetime import datetime, timezone
from statistics import median
from typing import Callable, Optional

from . import config, confidence
from .models import SENSORS, UNITS, Reading
from .parser import Sample
from .store import Store

WINDOW = {"heart_rate": 5, "spo2": 5, "temperature": 5, "weight": 5, "height": 5, "gsr": 5}
MAX_ATTEMPTS = 3
THRESHOLD = 0.60
MEASURING_TTL = 3.0  # seconds without samples -> measuring falls back to null


def _round_value(vtype: str, v: float):
    if vtype in ("heart_rate", "spo2"):
        return int(round(v))
    if vtype in ("weight", "gsr"):
        return round(v, 2)
    return round(v, 1)


class Aggregator:
    def __init__(self, store: Store, clock: Optional[Callable[[], datetime]] = None) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self._buf: dict[str, list[Sample]] = {}
        self._attempts: dict[str, int] = {}
        self._done: set[str] = set()

    def feed(self, sample: Sample) -> Optional[Reading]:
        with self._lock:
            vtype = sample.type
            if vtype in self._done:
                return None
            self._store.set_measuring(vtype, ttl=MEASURING_TTL)
            buf = self._buf.setdefault(vtype, [])
            buf.append(sample)
            continuous = config.continuous()
            if len(buf) < (3 if continuous else WINDOW[vtype]):  # demo: a new reading about every 3 s
                return None

            conf = confidence.score(vtype, buf)
            floor = config.confidence_floor()
            if floor and conf < floor:
                conf = floor  # DEMO switch, see config.confidence_floor
            reading = Reading(
                type=vtype,
                value=_round_value(vtype, median(s.value for s in buf)),
                unit=UNITS[vtype],
                confidence=conf,
                sensor=SENSORS.get(vtype),
                measured_at=self._clock(),
            )
            self._store.add(reading, max_keep=300 if continuous else 0)
            self._buf[vtype] = []
            self._attempts[vtype] = self._attempts.get(vtype, 0) + 1
            if not continuous and (conf >= THRESHOLD or self._attempts[vtype] >= MAX_ATTEMPTS):
                self._done.add(vtype)
            return reading
