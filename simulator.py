"""Simulation mode (SIMULATE=true): serves the full contract with no hardware.

A run takes ~45 s at SIM_SPEED=1, readings appear progressively, `measuring` walks
through the types and returns to null, SpO2 has a poor first attempt followed by a
good retake, and a blood_pressure object reading is included.
"""
import random
import threading
import time
from datetime import datetime, timezone

from .models import SENSORS, UNITS, BloodPressureValue, Reading
from .store import Store


def run(store: Store, stop: threading.Event, speed: float = 1.0) -> None:
    rng = random.Random()

    def reading(vtype: str, value, conf: float) -> None:
        if isinstance(value, dict):
            value = BloodPressureValue(**value)
        store.add(
            Reading(
                type=vtype,
                value=value,
                unit=UNITS[vtype],
                confidence=round(conf, 2),
                sensor=SENSORS.get(vtype),
                measured_at=datetime.now(timezone.utc),
            )
        )

    timeline = [
        (0.5, lambda: store.set_measuring("heart_rate")),
        (6.0, lambda: reading("heart_rate", 72 + rng.randint(-4, 4), rng.uniform(0.88, 0.96))),
        (6.5, lambda: store.set_measuring("spo2")),
        (11.0, lambda: reading("spo2", 97, rng.uniform(0.35, 0.50))),   # poor attempt
        (19.0, lambda: reading("spo2", 98, rng.uniform(0.85, 0.95))),   # retake
        (19.5, lambda: store.set_measuring("temperature")),
        (24.0, lambda: reading("temperature", round(36.4 + rng.random() * 0.5, 1), rng.uniform(0.88, 0.96))),
        (24.5, lambda: store.set_measuring("blood_pressure")),
        (38.0, lambda: reading("blood_pressure", {"systolic": 121, "diastolic": 79}, rng.uniform(0.84, 0.92))),
        (38.5, lambda: store.set_measuring("weight")),
        (44.0, lambda: reading("weight", round(68 + rng.random(), 2), rng.uniform(0.88, 0.96))),
        (44.5, lambda: reading("gsr", round(1400 + rng.random() * 20, 2), rng.uniform(0.88, 0.96))),
        (45.0, lambda: store.set_measuring(None)),
    ]

    start = time.monotonic()
    for t, action in timeline:
        wait = t / speed - (time.monotonic() - start)
        if wait > 0 and stop.wait(wait):
            return
        if stop.is_set():
            return
        action()
