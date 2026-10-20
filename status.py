"""Sensor status flags reported by the boards (finger on? electrodes on? ...)."""
import threading
import time


class StatusBoard:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._d: dict = {}

    def update(self, values: dict, age: float = 0.0) -> None:
        now = time.monotonic()
        with self._lock:
            for k, v in values.items():
                self._d[k] = (float(v), now - age)

    def snapshot(self) -> dict:
        """{flag: (value, seconds since it was reported)}"""
        now = time.monotonic()
        with self._lock:
            return {k: (v, now - t) for k, (v, t) in self._d.items()}

    def export(self) -> dict:
        return {k: [v, round(a, 2)] for k, (v, a) in self.snapshot().items()}

    def load(self, d: dict) -> None:
        for k, pair in d.items():
            try:
                self.update({k: float(pair[0])}, age=float(pair[1]))
            except (TypeError, ValueError, IndexError):
                continue
