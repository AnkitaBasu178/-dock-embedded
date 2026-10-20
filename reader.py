"""USB-serial reader. Runs in background threads, never inside an HTTP handler.

A supervisor re-scans for ports every 2 s and (re)starts a reader for any port that
is not currently being read, so an unplugged-and-replugged board recovers by itself.
Which ESP32 is on which port does not matter: every line says what it measures.
"""
import glob
import logging
import threading
from typing import Callable

logger = logging.getLogger("dock.reader")

AUTO_PATTERNS = [
    "/dev/cu.usbserial*",
    "/dev/cu.wchusbserial*",
    "/dev/cu.SLAB_USBtoUART*",
    "/dev/ttyUSB*",
    "/dev/ttyACM*",
]


def resolve_ports(spec: str) -> list[str]:
    if spec.strip().lower() == "auto":
        found: list[str] = []
        for pattern in AUTO_PATTERNS:
            found.extend(sorted(glob.glob(pattern)))
        return found
    return [p.strip() for p in spec.split(",") if p.strip()]


def _read_port(port: str, baud: int, on_line: Callable[[str], None], stop: threading.Event) -> None:
    try:
        import serial  # pyserial

        with serial.Serial(port, baud, timeout=1) as ser:
            logger.info("opened %s @ %d", port, baud)
            while not stop.is_set():
                raw = ser.readline()
                if not raw:
                    continue
                try:
                    on_line(raw.decode("utf-8", "replace"))
                except Exception:  # a bad line must never kill the reader
                    logger.exception("error handling line from %s", port)
    except Exception as exc:
        logger.warning("port %s unavailable (%s); will retry", port, exc)


class SerialSupervisor(threading.Thread):
    def __init__(self, on_line: Callable[[str], None], spec: str, baud: int, stop: threading.Event) -> None:
        super().__init__(name="serial-supervisor", daemon=True)
        self._on_line = on_line
        self._spec = spec
        self._baud = baud
        self._stop_evt = stop
        self._active: dict[str, threading.Thread] = {}

    def run(self) -> None:
        while not self._stop_evt.is_set():
            ports = resolve_ports(self._spec)
            if not ports:
                logger.info("no serial ports found yet (spec=%r)", self._spec)
            for port in ports:
                t = self._active.get(port)
                if t is None or not t.is_alive():
                    t = threading.Thread(
                        target=_read_port,
                        args=(port, self._baud, self._on_line, self._stop_evt),
                        name=f"serial-{port}",
                        daemon=True,
                    )
                    self._active[port] = t
                    t.start()
            self._stop_evt.wait(2.0)
