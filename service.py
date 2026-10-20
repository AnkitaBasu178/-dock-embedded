"""Glue: owns the store, the aggregator and the background workers."""
import logging
import threading
from datetime import datetime, timezone
from typing import Optional

import config, simulator
from aggregator import Aggregator
from check import sensor_check
from ecgbuf import EcgBuffer
from models import SENSOR_INFO
from parser import parse_ecg_line, parse_line, parse_status_line
from pusher import Pusher
from reader import SerialSupervisor
from status import StatusBoard
from store import Store

logger = logging.getLogger("dock.service")


class Service:
    def __init__(self, simulate: bool, relay: bool = False) -> None:
        self.simulate = simulate
        self.relay = relay
        self.store = Store()
        self.aggregator = Aggregator(self.store)
        self.ecg = EcgBuffer()
        self.status = StatusBoard()
        self.session = 0
        self.active = threading.Event()
        if simulate or config.autostart():
            self.active.set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._run_stop = threading.Event()
        self._sim_thread: Optional[threading.Thread] = None
        self._supervisor: Optional[SerialSupervisor] = None
        self._pusher: Optional[Pusher] = None

    # -- serial path -------------------------------------------------
    def ingest_line(self, line: str) -> None:
        if line.startswith("STATUS "):
            st = parse_status_line(line)
            if st:
                self.status.update(st)
            return
        if line.startswith("ECG "):
            vals = parse_ecg_line(line)
            if vals:
                self.ecg.extend(vals)
            return
        if not self.active.is_set():
            return  # sensors only record between /sensors/start and /sensors/stop
        sample = parse_line(line)
        if sample is not None:
            self.aggregator.feed(sample)

    # -- measuring session (the "turn on the sensors" route) -----------
    def start_session(self, session: Optional[int] = None) -> None:
        with self._lock:
            self.session = self.session + 1 if session is None else session
            self.store.clear()
            self.aggregator.reset()
            self.active.set()
        if self.simulate:
            self._run_stop.set()
            self._start_sim()

    def stop_session(self) -> None:
        self.active.clear()

    # -- outputs ---------------------------------------------------------
    def check(self) -> dict:
        return sensor_check(self.status.snapshot(), simulate=self.simulate)

    def latest_data(self) -> list:
        """One reading per sensor (the newest, and only while it is fresh)."""
        if not self.active.is_set():
            return []
        _, readings = self.store.snapshot()
        latest = {}
        for r in readings:
            latest[r.type] = r
        now = datetime.now(timezone.utc)
        out = []
        for info in SENSOR_INFO:
            r = latest.get(info["type"])
            if r is None:
                continue
            if not self.simulate and (now - r.measured_at).total_seconds() > config.fresh_seconds():
                continue
            out.append({
                "sensor_name": info["name"],
                "sensor_model": info["model"],
                "value": r.value,
                "unit": r.unit,
                "confidence": r.confidence,
                "timestamp": r.measured_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
        return out

    # -- lifecycle ---------------------------------------------------
    def start(self) -> None:
        if self.relay:
            logger.info("RELAY=true: waiting for data from the station via /ingest")
            return
        if self.simulate:
            logger.info("SIMULATE=true: no serial port will be opened")
            self._start_sim()
        else:
            self._supervisor = SerialSupervisor(
                self.ingest_line, config.serial_ports(), config.serial_baud(), self._stop
            )
            self._supervisor.start()
        if config.push_url() and not self.simulate:
            self._pusher = Pusher(self, config.push_url(), config.ingest_key() or "", self._stop)
            self._pusher.start()

    def stop(self) -> None:
        self._stop.set()
        self._run_stop.set()

    def restart_run(self) -> None:
        """Clear readings and re-arm. Called by POST /measure/start."""
        self._run_stop.set()
        self.store.clear()
        self.aggregator.reset()
        if self.simulate:
            self._start_sim()

    def _start_sim(self) -> None:
        self._run_stop = threading.Event()
        self._sim_thread = threading.Thread(
            target=simulator.run,
            args=(self.store, self._run_stop, config.sim_speed()),
            name="simulator",
            daemon=True,
        )
        self._sim_thread.start()
