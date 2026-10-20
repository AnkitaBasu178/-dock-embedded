"""DocK P5 (embedded) service: GET /health, GET /vitals, POST /measure/start."""
import secrets
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

import config
from models import ErrorBody, Reading, VitalsResponse
from service import Service


ECG_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Live ECG</title><style>
body{margin:0;font-family:system-ui,sans-serif;background:#0b1220;color:#e6edf3}
header{padding:12px 16px;display:flex;gap:16px;align-items:baseline;flex-wrap:wrap}
h1{font-size:18px;margin:0}#st{font-size:14px;opacity:.8}
canvas{width:100%;height:60vh;display:block;background:#0f1a2e}
.v{display:flex;gap:12px;padding:12px 16px;flex-wrap:wrap}
.c{background:#14223b;border-radius:8px;padding:8px 14px;min-width:120px}
.c b{display:block;font-size:22px}.c span{font-size:12px;opacity:.7}
</style></head><body>
<header><h1>Live ECG (AD8232)</h1><span id="st">connecting...</span></header>
<canvas id="cv"></canvas><div class="v" id="v"></div>
<script>
const key=new URLSearchParams(location.search).get('key')||'';
const q=key?('&key='+encodeURIComponent(key)):'';
const N=1500; let buf=[], total=-1;
const cv=document.getElementById('cv'), g=cv.getContext('2d');
function draw(){
  const w=cv.width=cv.clientWidth*devicePixelRatio, h=cv.height=cv.clientHeight*devicePixelRatio;
  g.clearRect(0,0,w,h);
  if(buf.length<2){return}
  let lo=Math.min(...buf), hi=Math.max(...buf); if(hi-lo<50){const m=(hi+lo)/2;lo=m-25;hi=m+25}
  g.strokeStyle='#3ddc84'; g.lineWidth=2*devicePixelRatio; g.beginPath();
  buf.forEach((y,i)=>{const px=i/(N-1)*w, py=h-(y-lo)/(hi-lo)*(h*0.9)-h*0.05; i?g.lineTo(px,py):g.moveTo(px,py)});
  g.stroke();
}
async function tick(){
  try{
    const r=await fetch('/ecg?since='+total+q,{headers:{'ngrok-skip-browser-warning':'1'}});
    const d=await r.json();
    if(d.samples){ if(total<0||d.total<total) buf=[]; buf=buf.concat(d.samples).slice(-N); total=d.total; }
    document.getElementById('st').textContent=d.live?'live':'no ECG signal (electrodes off or board unplugged)';
    draw();
  }catch(e){document.getElementById('st').textContent='cannot reach server'}
  try{
    const r=await fetch('/vitals?'+q.slice(1),{headers:{'ngrok-skip-browser-warning':'1'}});
    const d=await r.json();
    document.getElementById('v').innerHTML=(d.readings||[]).map(x=>`<div class="c"><span>${x.sensor||x.type}</span><b>${typeof x.value==='object'?x.value.systolic+'/'+x.value.diastolic:x.value} ${x.unit}</b><span>${x.type} · confidence ${x.confidence}</span></div>`).join('');
  }catch(e){}
}
setInterval(tick,1000); tick(); addEventListener('resize',draw);
</script></body></html>"""


LIVE_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Live readings</title><style>
body{margin:0;font-family:system-ui,sans-serif;background:#0b1220;color:#e6edf3}
h1{font-size:18px;margin:0;padding:12px 16px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:6px 16px;border-top:1px solid #1f2d4a}th{opacity:.6}
tr.new{background:#14304a}
</style></head><body><h1>Live readings (newest on top)</h1>
<table><thead><tr><th>time (UTC)</th><th>sensor</th><th>type</th><th>value</th><th>confidence</th></tr></thead><tbody id="b"></tbody></table>
<script>
const key=new URLSearchParams(location.search).get('key')||'';
const seen=new Set(); const b=document.getElementById('b');
async function tick(){
  try{
    const r=await fetch('/vitals'+(key?('?key='+encodeURIComponent(key)):''),{headers:{'ngrok-skip-browser-warning':'1'}});
    const d=await r.json();
    for(const x of (d.readings||[])){
      const id=x.type+x.measured_at; if(seen.has(id)) continue; seen.add(id);
      const v=typeof x.value==='object'?x.value.systolic+'/'+x.value.diastolic:x.value;
      const tr=document.createElement('tr'); tr.className='new';
      tr.innerHTML=`<td>${x.measured_at.slice(11,19)}</td><td>${x.sensor||''}</td><td>${x.type}</td><td>${v} ${x.unit}</td><td>${x.confidence}</td>`;
      b.insertBefore(tr,b.firstChild); setTimeout(()=>tr.className='',1500);
    }
  }catch(e){}
}
setInterval(tick,1000); tick();
</script></body></html>"""


def _error(status: int, error: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content=ErrorBody(error=error, message=message).model_dump())


def create_app(simulate: Optional[bool] = None, start_background: bool = True) -> FastAPI:
    sim = config.simulate() if simulate is None else simulate
    is_relay = config.relay()
    service = Service(simulate=sim and not is_relay, relay=is_relay)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_background:
            service.start()
        yield
        service.stop()

    app = FastAPI(title="DocK embedded (P5)", lifespan=lifespan)
    app.state.service = service

    @app.middleware("http")
    async def device_key_guard(request: Request, call_next):
        expected = config.device_key()
        if expected and request.url.path != "/health" and not request.url.path.startswith("/ingest"):
            # header for P4; ?key= only so a browser can open the graph page
            given = request.headers.get("X-Device-Key", "") or request.query_params.get("key", "")
            if not secrets.compare_digest(given.encode(), expected.encode()):
                return _error(401, "invalid_device_key", "missing or invalid X-Device-Key header")
        return await call_next(request)

    @app.exception_handler(StarletteHTTPException)
    async def http_exc(_: Request, exc: StarletteHTTPException):
        return _error(exc.status_code, "http_error", str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_exc(_: Request, exc: RequestValidationError):
        return _error(422, "invalid_request", str(exc.errors()))

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/vitals", response_model=VitalsResponse, response_model_exclude_none=True)
    def vitals() -> VitalsResponse:
        # Never touches hardware: returns whatever the store holds right now.
        measuring, readings = service.store.snapshot()
        hidden = config.hidden_types()
        if hidden:
            readings = [r for r in readings if r.type not in hidden]
            if measuring in hidden:
                measuring = None
        return VitalsResponse(measuring=measuring, readings=readings)

    @app.get("/data")
    def data() -> list:
        """THE url for P4: one reading per sensor: name, model, value, unit, confidence, timestamp."""
        return service.latest_data()

    @app.get("/sensors/check")
    def sensors_check() -> dict:
        return service.check()

    @app.api_route("/sensors/start", methods=["GET", "POST"])
    def sensors_start() -> dict:
        """Turns the measuring on (nothing to click on the station) and returns the sensor check."""
        service.start_session()
        return {"status": "started", "session": service.session, "check": service.check()}

    @app.api_route("/sensors/stop", methods=["GET", "POST"])
    def sensors_stop() -> dict:
        service.stop_session()
        return {"status": "stopped", "session": service.session}

    def _ingest_ok(request: Request) -> Optional[JSONResponse]:
        expected = config.ingest_key()
        if not expected:
            return _error(503, "ingest_disabled", "INGEST_KEY is not set on this server")
        given = request.headers.get("X-Ingest-Key", "")
        if not secrets.compare_digest(given.encode(), expected.encode()):
            return _error(401, "invalid_ingest_key", "missing or invalid X-Ingest-Key header")
        return None

    @app.get("/ingest/control")
    def ingest_control(request: Request):
        bad = _ingest_ok(request)
        if bad:
            return bad
        return {"active": service.active.is_set(), "session": service.session}

    @app.post("/ingest")
    async def ingest(request: Request):
        bad = _ingest_ok(request)
        if bad:
            return bad
        body = await request.json()
        for item in body.get("readings", []):
            try:
                service.store.add(Reading.model_validate(item), max_keep=300)
            except Exception:
                continue
        service.status.load(body.get("status", {}))
        ecg = body.get("ecg", {})
        if ecg.get("samples"):
            service.ecg.extend([int(x) for x in ecg["samples"]])
        return {"ok": True}

    @app.get("/ecg")
    def ecg(since: int = -1) -> dict:
        """Raw ECG for the graph. since=<total from the last call> returns only new samples."""
        total, samples, live = service.ecg.since(since)
        return {"sensor": "AD8232 ECG", "fs": 250, "unit": "adc", "total": total, "live": live, "samples": samples}

    @app.get("/ecg/plot", response_class=HTMLResponse)
    def ecg_plot() -> str:
        return ECG_PAGE

    @app.get("/live", response_class=HTMLResponse)
    def live_page() -> str:
        return LIVE_PAGE

    @app.post("/measure/start", status_code=202)
    def measure_start() -> dict:
        service.restart_run()
        return {"status": "started"}

    return app


app = create_app()
