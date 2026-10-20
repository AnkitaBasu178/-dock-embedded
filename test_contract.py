import logging
import time
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.aggregator import Aggregator
from app.main import create_app
from app.models import UNITS, Reading
from app.parser import parse_line
from app.service import Service
from app.store import Store

from .conftest import make_line

CLOSED_SET = set(UNITS)


# ---------------------------------------------------------------- health / empty
def test_health_ok():
    client = TestClient(create_app(simulate=False, start_background=False))
    assert client.get("/health").status_code == 200


def test_health_needs_no_key_even_when_key_set(monkeypatch):
    monkeypatch.setenv("DOCK_DEVICE_KEY", "secret")
    client = TestClient(create_app(simulate=False, start_background=False))
    assert client.get("/health").status_code == 200


def test_vitals_empty_is_200_and_empty_readings():
    client = TestClient(create_app(simulate=False, start_background=False))
    r = client.get("/vitals")
    assert r.status_code == 200
    assert r.json() == {"readings": []}


def test_service_starts_without_hardware_or_ports(monkeypatch):
    monkeypatch.setenv("SERIAL_PORTS", "/dev/does-not-exist")
    with TestClient(create_app(simulate=False)) as client:  # real start, no board
        assert client.get("/health").status_code == 200
        assert client.get("/vitals").json() == {"readings": []}


# ---------------------------------------------------------------- simulation
def _run_simulation():
    seen_counts, seen_measuring = [], []
    with TestClient(create_app(simulate=True)) as client:
        deadline = time.time() + 20
        body = {}
        while time.time() < deadline:
            body = client.get("/vitals").json()
            seen_counts.append(len(body["readings"]))
            seen_measuring.append(body.get("measuring"))
            if len(body["readings"]) >= 6 and body.get("measuring") is None:
                break
            time.sleep(0.03)
    return body, seen_counts, seen_measuring


def test_simulation_serves_valid_contract():
    body, counts, measuring = _run_simulation()
    assert body["readings"], "simulation produced nothing"
    for raw in body["readings"]:
        rd = Reading.model_validate(raw)  # strict: extra fields rejected
        assert rd.type in CLOSED_SET
        assert rd.unit == UNITS[rd.type]
        assert 0.0 <= rd.confidence <= 1.0
        assert raw["measured_at"].endswith("Z")
        ts = datetime.fromisoformat(raw["measured_at"].replace("Z", "+00:00"))
        assert ts.utcoffset() == timedelta(0)
    # progressive, not all at once
    assert len(set(counts)) > 3
    # measuring moves through types and returns to null
    types_seen = {m for m in measuring if m}
    assert {"heart_rate", "spo2", "blood_pressure"} <= types_seen
    assert measuring[-1] is None


def test_simulation_retake_case_and_blood_pressure():
    body, _, _ = _run_simulation()
    spo2 = [r for r in body["readings"] if r["type"] == "spo2"]
    assert len(spo2) >= 2
    assert spo2[0]["confidence"] < 0.60
    assert spo2[-1]["confidence"] >= 0.60
    assert spo2[0]["measured_at"] <= spo2[-1]["measured_at"]
    bp = [r for r in body["readings"] if r["type"] == "blood_pressure"]
    assert bp and set(bp[0]["value"]) == {"systolic", "diastolic"}


def test_measure_start_restarts_simulation():
    with TestClient(create_app(simulate=True)) as client:
        time.sleep(0.5)
        r = client.post("/measure/start")
        assert r.status_code == 202
        assert client.get("/vitals").json().get("readings", []) == []


# ---------------------------------------------------------------- auth
def test_key_unset_serves_everything():
    client = TestClient(create_app(simulate=False, start_background=False))
    assert client.get("/vitals").status_code == 200
    assert client.post("/measure/start").status_code == 202


def test_key_set_enforced(monkeypatch):
    monkeypatch.setenv("DOCK_DEVICE_KEY", "secret")
    client = TestClient(create_app(simulate=False, start_background=False))
    for headers in ({}, {"X-Device-Key": "wrong"}):
        r = client.get("/vitals", headers=headers)
        assert r.status_code == 401
        assert set(r.json()) == {"error", "message"}
        assert client.post("/measure/start", headers=headers).status_code == 401
    ok = client.get("/vitals", headers={"X-Device-Key": "secret"})
    assert ok.status_code == 200


def test_errors_use_one_shape():
    client = TestClient(create_app(simulate=False, start_background=False))
    r = client.get("/nope")
    assert r.status_code == 404
    assert set(r.json()) == {"error", "message"}


# ---------------------------------------------------------------- serial path
def test_malformed_lines_are_dropped_and_logged(caplog):
    caplog.set_level(logging.DEBUG, logger="dock.parser")
    good = make_line("VITAL type=spo2 value=98 ir=150000")
    bad_lines = [
        good[:-6],                               # truncated, no checksum
        good + good,                             # doubled
        "VITAL type=spo2 value=98 ir=150000*00",  # wrong checksum
        "\x00\x17VITAL" + good[5:],              # noise before data
        make_line("VITAL type=spo2 value=-999 ir=150000"),  # algorithm 'invalid' marker
        make_line("VITAL type=bp value=120"),    # not a serial type
        make_line("VITAL type=spo2 value=abc"),  # not a number
        make_line("VITAL type=spo2"),            # missing value
    ]
    service = Service(simulate=False)
    for line in bad_lines:
        assert parse_line(line) is None
        service.ingest_line(line)
    assert service.store.snapshot()[1] == []
    assert any(rec.levelno >= logging.WARNING for rec in caplog.records)


def test_boot_noise_is_ignored_quietly():
    assert parse_line("rst:0x1 (POWERON_RESET),boot:0x13") is None
    assert parse_line("") is None


def _feed(agg, vtype, values, ir=None):
    for v in values:
        extras = f" ir={ir}" if ir else ""
        s = parse_line(make_line(f"VITAL type={vtype} value={v}{extras}"))
        assert s is not None
        agg.feed(s)


def test_confidence_is_per_reading_not_constant():
    store = Store()
    agg = Aggregator(store)
    _feed(agg, "weight", [68.4, 68.45, 68.4, 68.42, 68.41])           # steady
    _feed(agg, "heart_rate", [60, 90, 75, 110, 65], ir=150000)         # erratic
    by_type = {r.type: r for r in store.snapshot()[1]}
    assert by_type["weight"].confidence > 0.9
    assert by_type["heart_rate"].confidence < 0.60
    assert by_type["weight"].confidence != by_type["heart_rate"].confidence


def test_weak_or_saturated_ir_lowers_confidence():
    good, weak, saturated = Store(), Store(), Store()
    for st, ir in ((good, 120000), (weak, 20000), (saturated, 255000)):
        _feed(Aggregator(st), "spo2", [98, 98, 97, 98, 98], ir=ir)
    g, w, s = (st.snapshot()[1][0].confidence for st in (good, weak, saturated))
    assert g > 0.9 and w < g and s < 0.60


def test_poor_reading_then_retake_both_kept_max_three_attempts():
    store = Store()
    agg = Aggregator(store)
    _feed(agg, "spo2", [85, 98, 90, 99, 80], ir=150000)    # poor
    _feed(agg, "spo2", [98, 98, 97, 98, 98], ir=150000)    # retake
    _feed(agg, "spo2", [98, 98, 98, 98, 98], ir=150000)    # ignored: already good
    spo2 = [r for r in store.snapshot()[1] if r.type == "spo2"]
    assert len(spo2) == 2
    assert spo2[0].confidence < 0.60 <= spo2[1].confidence

    store2 = Store()
    agg2 = Aggregator(store2)
    for _ in range(5):                                      # never gets good
        _feed(agg2, "spo2", [70, 99, 80, 95, 85], ir=150000)
    assert len(store2.snapshot()[1]) == 3                   # capped at 3 attempts


def test_temperature_arrives_as_a_plain_reading():
    store = Store()
    _feed(Aggregator(store), "temperature", [36.45, 36.5, 36.52, 36.48, 36.5])
    r = store.snapshot()[1][0]
    assert r.type == "temperature" and r.unit == "C" and 36.4 < r.value < 36.6
    assert r.confidence > 0.9


def test_gsr_is_a_valid_type_in_mV():
    store = Store()
    _feed(Aggregator(store), "gsr", [1410, 1412, 1409, 1411, 1410])
    r = store.snapshot()[1][0]
    assert r.type == "gsr" and r.unit == "mV" and r.confidence > 0.9
    Reading.model_validate(r.model_dump(mode="json"))


def test_gsr_confidence_uses_relative_spread():
    steady, jumpy = Store(), Store()
    _feed(Aggregator(steady), "gsr", [1500, 1510, 1505, 1498, 1502])   # ~2.5 % spread
    _feed(Aggregator(jumpy), "gsr", [900, 1500, 700, 1700, 1100])         # huge relative spread
    assert steady.snapshot()[1][0].confidence > 0.9
    assert jumpy.snapshot()[1][0].confidence < 0.6


def test_ecg_heart_rate_confidence_follows_rr_regularity():
    good, bad = Store(), Store()
    for st, cv in ((good, 0.04), (bad, 0.33)):
        agg = Aggregator(st)
        for v in (72, 73, 72, 72, 73):
            agg.feed(parse_line(make_line(f"VITAL type=heart_rate value={v} rrcv={cv}")))
    assert good.snapshot()[1][0].confidence > 0.9
    assert bad.snapshot()[1][0].confidence < 0.6


def test_ecg_heart_rate_confidence_uses_q():
    good, weak = Store(), Store()
    for st, q in ((good, 0.92), (weak, 0.30)):
        agg = Aggregator(st)
        for v in (72, 73, 72, 72, 73):
            agg.feed(parse_line(make_line(f"VITAL type=heart_rate value={v} q={q}")))
    assert good.snapshot()[1][0].confidence > 0.9
    assert weak.snapshot()[1][0].confidence < 0.6


def test_hide_types_keeps_gsr_out_of_vitals(monkeypatch):
    monkeypatch.setenv("HIDE_TYPES", "gsr")
    app = create_app(simulate=False, start_background=False)
    svc = app.state.service
    for v in (1410, 1410, 1410, 1410, 1410):
        svc.ingest_line(make_line(f"VITAL type=gsr value={v}"))
    for v in (36.5,) * 5:
        svc.ingest_line(make_line(f"VITAL type=temperature value={v}"))
    types = [r["type"] for r in TestClient(app).get("/vitals").json()["readings"]]
    assert types == ["temperature"]
    monkeypatch.delenv("HIDE_TYPES")
    types = [r["type"] for r in TestClient(app).get("/vitals").json()["readings"]]
    assert set(types) == {"gsr", "temperature"}


def test_measuring_falls_back_to_null_without_samples():
    store = Store()
    store.set_measuring("spo2", ttl=0.05)
    assert store.snapshot()[0] == "spo2"
    time.sleep(0.1)
    assert store.snapshot()[0] is None


def test_ecg_lines_feed_graph_endpoint():
    from app.parser import checksum
    app = create_app(simulate=False, start_background=False)
    body = "ECG d=2000,2010,2100,3000,2050"
    app.state.service.ingest_line(f"{body}*{checksum(body):02X}")
    app.state.service.ingest_line(f"{body}*00")  # bad checksum: dropped
    c = TestClient(app)
    d = c.get("/ecg").json()
    assert d["samples"] == [2000, 2010, 2100, 3000, 2050] and d["live"] and d["total"] == 5
    assert c.get(f"/ecg?since={d['total']}").json()["samples"] == []
    assert "Live ECG" in c.get("/ecg/plot").text


def test_sensor_name_floor_and_continuous(monkeypatch):
    monkeypatch.setenv("CONFIDENCE_FLOOR", "0.89")
    monkeypatch.setenv("CONTINUOUS", "true")
    store = Store()
    agg = Aggregator(store)
    _feed(agg, "spo2", [70, 99, 80, 95, 85], ir=150000)    # erratic -> real confidence is low
    _feed(agg, "spo2", [97, 97, 97, 97, 97], ir=150000)    # keeps going, new reading every 3 samples
    rs = [r for r in store.snapshot()[1] if r.type == "spo2"]
    assert len(rs) >= 3 and rs[-1].value == 97               # a growing log, newest last
    assert all(r.confidence >= 0.89 for r in rs) and rs[0].sensor == "MAX30102"


# ---------------------------------------------------------------- P4's simple /data format
def _feed_lines(svc, vtype, vals, extra=""):
    for v in vals:
        svc.ingest_line(make_line(f"VITAL type={vtype} value={v}{extra}"))


def test_data_is_one_reading_per_sensor_in_the_exact_format(monkeypatch):
    monkeypatch.setenv("CONTINUOUS", "true")
    app = create_app(simulate=False, start_background=False)
    svc = app.state.service
    _feed_lines(svc, "spo2", [97] * 9, " ir=150000")     # three readings of the same sensor
    _feed_lines(svc, "gsr", [1400] * 3)
    d = TestClient(app).get("/data").json()
    assert [x["sensor_name"] for x in d] == ["SpO2", "GSR"]            # one each, fixed order
    assert set(d[0]) == {"sensor_name", "sensor_model", "value", "unit", "confidence", "timestamp"}
    assert d[0]["sensor_model"] == "MAX30102" and d[0]["unit"] == "%" and d[0]["value"] == 97
    assert d[0]["timestamp"].endswith("Z")


def test_data_drops_a_sensor_that_stopped_reading(monkeypatch):
    monkeypatch.setenv("FRESH_SECONDS", "0")
    app = create_app(simulate=False, start_background=False)
    _feed_lines(app.state.service, "gsr", [1400] * 5)
    time.sleep(0.05)
    assert TestClient(app).get("/data").json() == []


def test_nothing_is_recorded_until_sensors_start(monkeypatch):
    monkeypatch.delenv("AUTOSTART")
    app = create_app(simulate=False, start_background=False)
    svc = app.state.service
    c = TestClient(app)
    _feed_lines(svc, "gsr", [1400] * 5)
    assert c.get("/data").json() == []
    r = c.post("/sensors/start").json()
    assert r["status"] == "started" and r["session"] == 1 and "check" in r
    _feed_lines(svc, "gsr", [1400] * 5)
    assert [x["sensor_name"] for x in c.get("/data").json()] == ["GSR"]
    assert c.get("/sensors/start").status_code == 200       # a plain browser visit works too
    assert c.get("/data").json() == []                        # a new session starts clean
    c.post("/sensors/stop")
    _feed_lines(svc, "gsr", [1400] * 5)
    assert c.get("/data").json() == []


def test_sensor_check_reports_each_sensor():
    app = create_app(simulate=False, start_background=False)
    svc = app.state.service
    c = TestClient(app)
    assert all(s["status"] == "not_connected" for s in c.get("/sensors/check").json()["sensors"])
    svc.ingest_line(make_line("STATUS max30102=1 finger=0 ecg=1 gsr=0"))
    svc.ingest_line(make_line("STATUS hx711=1 weight=0"))
    by = {s["sensor_name"]: s["status"] for s in c.get("/sensors/check").json()["sensors"]}
    assert by == {"SpO2": "waiting", "Temperature": "waiting", "Heart Rate": "ok",
                  "GSR": "waiting", "Weight": "waiting"}
    svc.ingest_line(make_line("STATUS max30102=0 finger=0 ecg=0 gsr=1"))
    by = {s["sensor_name"]: s["status"] for s in c.get("/sensors/check").json()["sensors"]}
    assert by["SpO2"] == "not_connected" and by["GSR"] == "ok" and by["Heart Rate"] == "waiting"
    svc.ingest_line("STATUS max30102=1*00")                   # bad checksum: ignored
    assert c.get("/sensors/check").json()["all_ok"] is False


def test_station_pushes_to_the_cloud_copy(monkeypatch):
    """Mac (bridge) -> Render copy (relay), all in one process on a real local port."""
    import socket
    import threading
    import uvicorn

    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    monkeypatch.setenv("INGEST_KEY", "k3y")
    monkeypatch.setenv("RELAY", "true")
    cloud = create_app(start_background=False)
    server = uvicorn.Server(uvicorn.Config(cloud, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)

    monkeypatch.delenv("RELAY")
    monkeypatch.setenv("PUSH_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("SERIAL_PORTS", "/nonexistent")
    monkeypatch.delenv("AUTOSTART")
    station = Service(simulate=False)
    station.start()
    try:
        ctl = TestClient(cloud)
        assert ctl.get("/data").json() == []
        assert ctl.get("/ingest/control").status_code == 401            # needs the key
        cloud.state.service.start_session()                              # P4 calls /sensors/start
        for _ in range(60):
            if station.active.is_set():
                break
            time.sleep(0.1)
        assert station.active.is_set() and station.session == cloud.state.service.session
        station.ingest_line(make_line("STATUS max30102=1 finger=1 ecg=1 gsr=1"))
        _feed_lines(station, "gsr", [1400] * 5)
        station.ingest_line(make_line("ECG d=2000,2010,2020"))
        for _ in range(60):
            if ctl.get("/data").json():
                break
            time.sleep(0.1)
        d = ctl.get("/data").json()
        assert [x["sensor_name"] for x in d] == ["GSR"] and d[0]["unit"] == "mV"
        assert ctl.get("/sensors/check").json()["sensors"][3]["status"] == "ok"
        assert ctl.get("/ecg").json()["samples"][-3:] == [2000, 2010, 2020]
    finally:
        station.stop()
        server.should_exit = True
