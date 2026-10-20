"""Sensor check: is each sensor present and ready to measure?  (No clinical meaning.)"""
from models import SENSOR_INFO

FRESH = 6.0  # a board that has not reported for this many seconds counts as not connected


def sensor_check(snap: dict, simulate: bool = False) -> dict:
    def get(key):
        v = snap.get(key)
        return None if (v is None or v[1] > FRESH) else v[0]

    main_alive = any(get(k) is not None for k in ("max30102", "finger", "ecg", "gsr"))
    load_alive = get("hx711") is not None
    sensors = []
    for info in SENSOR_INFO:
        t = info["type"]
        if simulate:
            status, detail = "ok", "simulation"
        elif t in ("spo2", "temperature"):
            if not main_alive:
                status, detail = "not_connected", "sensor board (ESP #1) is not sending"
            elif get("max30102") == 0:
                status, detail = "not_connected", "MAX30102 not found on the board"
            elif not get("finger"):
                status, detail = "waiting", "put a finger on the pulse sensor"
            else:
                status, detail = "ok", "ready"
        elif t == "heart_rate":
            if not main_alive:
                status, detail = "not_connected", "sensor board (ESP #1) is not sending"
            elif not get("ecg"):
                status, detail = "waiting", "attach the ECG electrodes"
            else:
                status, detail = "ok", "ready"
        elif t == "gsr":
            if not main_alive:
                status, detail = "not_connected", "sensor board (ESP #1) is not sending"
            elif not get("gsr"):
                status, detail = "waiting", "touch the GSR electrodes"
            else:
                status, detail = "ok", "ready"
        else:  # weight
            if not load_alive:
                status, detail = "not_connected", "load-cell board (ESP #2) is not sending"
            elif get("hx711") == 0:
                status, detail = "not_connected", "HX711 not found on the board"
            elif not get("weight"):
                status, detail = "waiting", "step on the platform"
            else:
                status, detail = "ok", "ready"
        sensors.append({"sensor_name": info["name"], "sensor_model": info["model"],
                        "status": status, "detail": detail})
    return {"all_ok": all(s["status"] == "ok" for s in sensors), "sensors": sensors}
