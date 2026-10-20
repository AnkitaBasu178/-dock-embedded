"""Thread-safe in-memory store of the latest readings. HTTP handlers only read this."""
import threading
import time
from collections import deque
from typing import Optional

from .models import Reading


class Store:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._readings: list[Reading] = []
        self._recent: deque = deque(maxlen=500)  # (seq, reading) for the Render pusher
        self._seq = 0
        self._measuring: Optional[str] = None
        self._measuring_until: Optional[float] = None

    def add(self, reading: Reading, max_keep: int = 0) -> None:
        """max_keep > 0 (demo): keep a rolling log, dropping the oldest beyond that many."""
        with self._lock:
            self._readings.append(reading)
            self._seq += 1
            self._recent.append((self._seq, reading))
            if max_keep and len(self._readings) > max_keep:
                del self._readings[: len(self._readings) - max_keep]

    def since(self, seq: int) -> tuple:
        """(latest seq, readings added after `seq`)"""
        with self._lock:
            return self._seq, [r for s, r in self._recent if s > seq]

    def clear(self) -> None:
        with self._lock:
            self._readings.clear()
            self._recent.clear()
            self._measuring = None
            self._measuring_until = None

    def set_measuring(self, vtype: Optional[str], ttl: Optional[float] = None) -> None:
        """ttl=None keeps it until changed; otherwise it expires on its own, so an
        unplugged board automatically makes `measuring` fall back to null."""
        with self._lock:
            self._measuring = vtype
            self._measuring_until = None if (vtype is None or ttl is None) else time.monotonic() + ttl

    def snapshot(self) -> tuple[Optional[str], list[Reading]]:
        with self._lock:
            if self._measuring_until is not None and time.monotonic() > self._measuring_until:
                self._measuring = None
                self._measuring_until = None
            return self._measuring, list(self._readings)
