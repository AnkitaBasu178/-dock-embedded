# DocK embedded service (P5)

Standalone FastAPI service that reads the station's sensors over **USB serial** and
serves them to the DocK platform (P4) as JSON. Contract: `P5_EMBEDDED_AGENT.md`.

```
ESP32 #1 (MAX30102 + AD8232 ECG + GSR) --USB-->  ┐
ESP32 #2 (HX711)     --USB-->  ├ this Mac:  reader threads -> in-memory store -> GET /vitals
                               ┘
P4's laptop --WiFi--> http://<this-mac-ip>:8010
```

The ESP32s use no WiFi and have no clock. This service stamps `measured_at`
(UTC) when a line arrives, so timing never depends on a network.

## Run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

SIMULATE=true uvicorn app.main:app --host 0.0.0.0 --port 8010   # no hardware needed
uvicorn app.main:app --host 0.0.0.0 --port 8010                 # with the ESP32s plugged in
pytest
```

```bash
curl http://localhost:8010/health
curl http://localhost:8010/vitals
```

`--host 0.0.0.0` is required so P4's laptop can reach it. Find your Mac's IP with
`ipconfig getifaddr en0` and give P4 `http://<that-ip>:8010`.

## Configuration

| variable | default | purpose |
|---|---|---|
| `PORT` | `8010` | port, only used by `python -m app` (with uvicorn use `--port`) |
| `DOCK_DEVICE_KEY` | unset | if set, every endpoint except `/health` requires header `X-Device-Key` |
| `SIMULATE` | unset | `true` = fake sensors, no serial port opened |
| `SERIAL_PORTS` | `auto` | `auto` scans `/dev/cu.usbserial*`, `/dev/cu.wchusbserial*`, `/dev/cu.SLAB_USBtoUART*`, `/dev/ttyUSB*`, `/dev/ttyACM*`; or a comma list e.g. `/dev/cu.usbserial-0001,/dev/cu.usbserial-0002` |
| `SERIAL_BAUD` | `115200` | baud rate of both boards |
| `SIM_SPEED` | `1` | simulation speed multiplier (1 = ~45 s run) |
| `CONTINUOUS` | unset | `true` = never stop after a good reading; `/vitals` always shows the newest reading of each type (demo) |
| `CONFIDENCE_FLOOR` | unset | DEMO ONLY: e.g. `0.89` lifts every confidence to at least that. Real value is computed, then raised. Tell P4 if you use it |
| `HIDE_TYPES` | unset | comma list of types to keep out of `/vitals`, e.g. `gsr` until P4 supports it |

Which board is on which port does not matter: every line says what it measures.

## Serial line format (ESP32 -> Mac)

```
VITAL type=spo2 value=98 ir=150000*5A
VITAL type=weight value=68.42*3C
VITAL type=heart_rate value=72.00 q=0.91*xx   (from the ECG; q = rhythm quality)
VITAL type=gsr value=1410.50*xx
```

`*HH` = XOR of every byte before the `*`. A line that is truncated, doubled, has
noise in front, a wrong checksum, a non-numeric or physically impossible value
(e.g. SpO2 `-999`) is **dropped and logged**, never turned into a reading.
Lines that do not start with `VITAL` (boot messages) are ignored. Sketches are in
`firmware/`.

## How confidence is computed (per reading, never constant)

Each reading is the median of a window of 5 samples. `confidence = signal x stability`.

- **stability**: spread (max-min) inside the window. 1.0 when tight, falling to 0.1
  when erratic. Thresholds: heart_rate 3 to 15 bpm, spo2 2 to 8 points, weight 0.3 to 3 kg,
  temperature 0.2 to 1.0 C.
- **signal** (heart_rate/spo2 only): for spo2 the MAX30102 IR level; for the ECG heart_rate the `q` rhythm quality the firmware measures (0.8+ = clean; 0.7 is the firmware cutoff, below it nothing is sent). GSR stability uses the spread relative to its median. MAX30102 IR level: 1.0 for 40k to 200k, falling
  to 0.5 at 250k, 0.3 when saturated or weak, 0.1 with no finger.

No ranges or verdicts: a 190/120 would be reported like 118/76.

## Retakes

If a reading is below 0.60, the next window is an automatic retake. Both are kept as
two entries with the same `type`. Maximum 3 attempts per type; after a reading
>= 0.60 (or the 3rd attempt) that type stops until `POST /measure/start`.

## Render (cloud copy) = the one permanent URL for P4

```
ESP32s --USB--> Mac (this service, run_demo.sh) --pushes every 1 s--> Render --> P4: GET /data
```

P4's routes (all JSON, same on the Mac and on Render):

| route | what it does |
|---|---|
| `POST` or `GET /sensors/start` | turns the measuring on (nothing to click on the station), starts a clean session, returns the sensor check |
| `GET /sensors/check` | per sensor: `ok` / `waiting` (e.g. no finger) / `not_connected`, with a plain-words detail |
| `GET /data` | **one reading per sensor**, fixed order, only sensors that are reading right now: `sensor_name, sensor_model, value, unit, confidence, timestamp` |
| `POST` or `GET /sensors/stop` | stops measuring |
| `GET /ecg/plot` | live ECG graph page |

Readings are only recorded between `/sensors/start` and `/sensors/stop`. A sensor that stops
reading (finger removed) drops out of `/data` after `FRESH_SECONDS` (15). `/vitals` is still the old
contract route. Render free tier sleeps after ~15 min without requests; while the Mac is pushing
it never sleeps, and after a sleep the first request takes about a minute to wake it.

### One-time Render setup
1. Code on GitHub (branch with the `embedded/` folder). Render: New + > Blueprint > pick the repo > it reads `render.yaml`.
2. After the first deploy, Render > the service > Environment: copy `INGEST_KEY`.
3. Your URL is `https://dock-embedded.onrender.com` (or the name Render shows).
4. On the Mac create `embedded/bridge.env` with two lines: `PUSH_URL=<that url>` and `INGEST_KEY=<the key>`. `bash run_demo.sh` then pushes automatically.

## Alternative: ngrok (no cloud) and live ECG graph

1. Sign up at ngrok.com, download the Mac zip, unzip it, then `./ngrok config add-authtoken <token from dashboard>`.
2. Dashboard -> Domains: your free permanent domain (e.g. `xxxx.ngrok-free.dev`) is already there.
3. Window 1: `./run_demo.sh`. Window 2: `./ngrok http --url=xxxx.ngrok-free.dev 8010`.
4. Same link forever: `https://xxxx.ngrok-free.dev/vitals`, graph at `/ecg/plot`.
   Browsers see an ngrok warning page once (click Visit); API calls are not affected.

In demo mode a new reading per type arrives about every 3 s and `/vitals` is a growing log (newest last, last 300 kept). `/live` is a page that shows them appearing one by one.

Every reading in `/vitals` has `sensor` (e.g. `MAX30102`, `AD8232 ECG`). `GET /ecg?since=<n>` returns raw ECG samples (250/s) for the graph.

## Endpoints

- `GET /health` -> 200, never needs a key
- `GET /vitals` -> 200 always. `measuring` is omitted when idle; `readings` is `[]` before the first one
- `POST /measure/start` -> 202. Clears readings and re-arms every type (restarts the simulated run in simulation)
- Errors: `{"error": "...", "message": "..."}` with a 4xx/5xx status

## Troubleshooting

| symptom | likely cause |
|---|---|
| `/vitals` is `{"readings": []}` forever | wrong/missing port: check `ls /dev/cu.*`, or set `SERIAL_PORTS` |
| log says `port ... unavailable` | **Arduino Serial Monitor is open** (only one program can own a port): close it |
| no readings from the load cells | nobody on the platform: nothing is sent below 5 kg (`MIN_PERSON_KG`) |
| no SpO2 / temperature | no finger on the MAX30102 (IR < 50000): nothing is sent |
| no heart_rate | ECG electrodes off (LO+/LO-), or the 8 s window is not yet full/clean: nothing is sent (first value ~8 s after contact) |
| no gsr | fingers not on the GSR electrodes, or they were touched at power-up (re-press EN with fingers off) |
| readings stop after unplugging a board | wait ~2 s; the supervisor re-scans and reconnects on its own |
| `401` on every call | `DOCK_DEVICE_KEY` set here but P4 sends a different value |
| works here, fails from P4 | not started with `--host 0.0.0.0`, different WiFi (or college WiFi isolating clients: use a phone hotspot), or Mac firewall |
| ESP32 resets when the service starts | normal: opening the port toggles DTR/RTS |

## Handover notes (for the PR / commit message)

1. **Base URL / port**: `http://<mac-ip>:8010`, change with `--port`.
2. **Real captured `/vitals`**: TO BE FILLED after the hardware run (the simulated capture is not real).
3. **Types produced**: `heart_rate` (from an AD8232 ECG, not a finger sensor), `spo2`, `weight`, and `gsr` (**extension, not in P4's list**: skin sensor output, raw millivolts, `unit: "mV"`; P4 must add it or run with `HIDE_TYPES=gsr`). `temperature` is simulated for now (the MLX90614 is not detected; P4 already knows). It is sent only together with SpO2 data, so no finger means no temperature either. `height` and `blood_pressure`: no sensor. `blood_pressure` appears **only in simulation**.
4. **Confidence**: see above.
5. **Auto retake**: yes, up to 3 attempts per type, below 0.60.
6. **Cadence**: about 5 s per attempt for SpO2/HR (one sample per second) and about 5 s for weight (one sample per ~1 s, each a 9-read median); tune `WINDOW` in `app/aggregator.py`. The two boards run in parallel, and the order is whatever the patient does. To be confirmed on the real hardware.
7. **`POST /measure/start`**: yes. Sensors measure continuously, but a finished type stops reporting, so P4 needs a way to start a fresh run for the next patient.
8. **Link**: USB serial, 115200 baud, ports auto-detected on macOS.
9. **Simulation**: yes, `SIMULATE=true uvicorn app.main:app --host 0.0.0.0 --port 8010`.
10. **Doc vs hardware**: no blood-pressure cuff or height sensor exists on this station; `measuring` is omitted rather than `null` when idle.
