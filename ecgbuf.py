"""Last few seconds of raw ECG (for the live graph). Thread-safe."""
import threading
import time
from collections import deque

FS = 250


class EcgBuffer:
    def __init__(self, seconds: int = 12) -> None:
        self._lock = threading.Lock()
        self._d: deque = deque(maxlen=FS * seconds)
        self._total = 0
        self._last = 0.0

    def extend(self, vals: list) -> None:
        with self._lock:
            self._d.extend(vals)
            self._total += len(vals)
            self._last = time.monotonic()

    def since(self, total: int) -> tuple:
        """(new total, samples added after `total`, live?)"""
        with self._lock:
            new = min(self._total - total, len(self._d)) if total >= 0 else len(self._d)
            new = max(new, 0)
            samples = list(self._d)[len(self._d) - new:] if new else []
            live = (time.monotonic() - self._last) < 3.0 and self._total > 0
            return self._total, samples, live
