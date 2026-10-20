"""Runs on the Mac: pushes readings/status/ECG to the Render copy and obeys its start/stop."""
import json
import logging
import threading
import urllib.request

logger = logging.getLogger("dock.pusher")


class Pusher(threading.Thread):
    def __init__(self, service, url: str, key: str, stop: threading.Event) -> None:
        super().__init__(name="pusher", daemon=True)
        self._svc, self._url, self._key, self._stop_ev = service, url, key, stop
        self._seq = 0
        self._ecg_total = -1
        self._ok = None

    def _call(self, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self._url + path, data=data,
            headers={"X-Ingest-Key": self._key, "Content-Type": "application/json"},
            method="POST" if body is not None else "GET",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode() or "{}")

    def run(self) -> None:
        svc = self._svc
        while not self._stop_ev.wait(1.0):
            try:
                ctl = self._call("/ingest/control")
                if ctl.get("active"):
                    if (not svc.active.is_set()) or ctl.get("session") != svc.session:
                        svc.start_session(session=ctl.get("session"))
                        self._seq = 0
                else:
                    svc.stop_session()
                seq, new = svc.store.since(self._seq) if svc.active.is_set() else (self._seq, [])
                total, samples, _ = svc.ecg.since(self._ecg_total)
                body = {
                    "readings": [r.model_dump(mode="json") for r in new],
                    "status": svc.status.export(),
                    "ecg": {"total": total, "samples": samples},
                }
                self._call("/ingest", body)
                self._seq, self._ecg_total = seq, total
                if self._ok is not True:
                    logger.warning("connected to %s", self._url)
                    self._ok = True
            except Exception as exc:  # network down, Render waking up, wrong key...
                if self._ok is not False:
                    logger.warning("cannot reach %s: %s", self._url, exc)
                    self._ok = False
