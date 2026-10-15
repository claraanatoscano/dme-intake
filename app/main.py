"""HTTP layer: webhook intake, status dashboard, run API, replay, health."""
import base64
import hmac
import logging
import re
import threading
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select

from . import config, dashboard, pipeline
from .db import Run, SessionLocal, init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("intake.api")


def _sweeper():
    while True:
        try:
            n = pipeline.sweep_stuck_runs()
            if n:
                log.warning("marked %s stuck run(s) as failed", n)
        except Exception:
            log.exception("sweeper error")
        time.sleep(120)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    threading.Thread(target=_sweeper, daemon=True).start()
    yield


app = FastAPI(title="DME Order Intake", lifespan=lifespan)


def _authorized(request: Request, secret: str) -> bool:
    if not secret:
        return False
    supplied = request.headers.get("x-api-key") or request.query_params.get("key") or ""
    return hmac.compare_digest(supplied.encode(), secret.encode())


def _drive_to_direct(url: str) -> str:
    m = re.search(r"drive\.google\.com/file/d/([^/]+)", url) or re.search(r"[?&]id=([^&]+)", url)
    if "drive.google.com" in url and m:
        return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    return url


def _fetch_url(url: str) -> bytes:
    if not url.lower().startswith("https://"):
        raise HTTPException(400, "file_url must be https")
    limit = int(config.MAX_PDF_MB * 1024 * 1024)
    with httpx.stream("GET", _drive_to_direct(url), follow_redirects=True, timeout=30) as resp:
        if resp.status_code >= 400:
            raise HTTPException(400, f"Could not download file_url (HTTP {resp.status_code})")
        buf = bytearray()
        for chunk in resp.iter_bytes():
            buf.extend(chunk)
            if len(buf) > limit:
                raise HTTPException(413, "File too large")
    return bytes(buf)


async def _read_document(request: Request) -> tuple[bytes, str]:
    """Accepts multipart (field 'file'), raw application/pdf, or JSON {content_base64|file_url, filename}."""
    ctype = request.headers.get("content-type", "").lower()
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file") or next((v for v in form.values() if hasattr(v, "read")), None)
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(400, "Multipart request must include a file field named 'file'")
        return await upload.read(), (upload.filename or "document.pdf")
    if ctype.startswith("application/json"):
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON body")
        name = str(body.get("filename") or "document.pdf")
        if body.get("content_base64"):
            try:
                return base64.b64decode(body["content_base64"], validate=False), name
            except Exception:
                raise HTTPException(400, "content_base64 is not valid base64")
        if body.get("file_url"):
            url = str(body["file_url"])
            return _fetch_url(url), body.get("filename") or url.rstrip("/").split("/")[-1][:100] or "document.pdf"
        raise HTTPException(400, "JSON body needs content_base64 or file_url")
    data = await request.body()
    name = request.headers.get("x-filename") or request.query_params.get("filename") or "document.pdf"
    return data, name


@app.get("/webhook/orders")
def webhook_help():
    return {
        "how_to_use": "POST a PDF to this URL with header X-API-Key.",
        "formats": ["multipart/form-data with a field named 'file'", "raw body with Content-Type: application/pdf",
                    "JSON {\"file_url\": \"https://...\"} (Google Drive share links work)",
                    "JSON {\"content_base64\": \"...\", \"filename\": \"order.pdf\"}"],
        "example": f'curl -X POST "{config.PUBLIC_BASE_URL}/webhook/orders" -H "X-API-Key: <key>" -F "file=@order.pdf"',
        "dashboard": f"{config.PUBLIC_BASE_URL}/status",
    }


@app.post("/webhook/orders")
async def receive_order(request: Request, background: BackgroundTasks):
    if not _authorized(request, config.INTAKE_API_KEY):
        raise HTTPException(401, "Missing or invalid API key (send header X-API-Key or ?key=)")
    channel = re.sub(r"[^a-z0-9_:-]", "", (request.query_params.get("channel") or "webhook").lower())[:32] or "webhook"
    data, filename = await _read_document(request)
    if len(data) > config.MAX_PDF_MB * 1024 * 1024:
        raise HTTPException(413, "File too large")
    try:
        run, should_process = pipeline.intake(data, filename, channel)
    except pipeline.DailyLimitReached as exc:
        raise HTTPException(429, str(exc))
    body = {"run_id": run.id, "status": run.status, "status_url": f"{config.PUBLIC_BASE_URL}/runs/{run.id}",
            "dashboard": f"{config.PUBLIC_BASE_URL}/status#run-{run.id}"}
    if not should_process:
        body.update(duplicate_of=run.duplicate_of, record_url=run.record_url,
                    message="Duplicate document: no new record was created.")
        return JSONResponse(body, status_code=200)
    background.add_task(pipeline.process, run.id)
    body["message"] = "Accepted. Processing in the background; poll status_url for the result."
    return JSONResponse(body, status_code=202)


def _run_json(r: Run, reveal: bool) -> dict:
    fields = dict(r.fields or {})
    if not reveal and fields:
        name, dob = dashboard._mask(fields, False)
        fields.update(patient_first_name=name.split(" ")[0], patient_last_name=name.split(" ")[-1], patient_dob=dob)
    return {"run_id": r.id, "status": r.status, "channel": r.channel, "filename": r.filename,
            "created_at": r.created_at.isoformat(), "duration_ms": r.duration_ms, "attempts": r.attempts,
            "error": r.error, "missing": r.missing, "fields": fields, "record_url": r.record_url,
            "duplicate_of": r.duplicate_of, "replay_of": r.replay_of}


def _recent(limit: int = 100) -> list[Run]:
    with SessionLocal() as s:
        return list(s.scalars(select(Run).order_by(Run.created_at.desc()).limit(limit)))


@app.get("/runs/{run_id}")
def get_run(run_id: str, request: Request):
    with SessionLocal() as s:
        r = s.get(Run, run_id)
        if not r:
            raise HTTPException(404, "Run not found")
        return _run_json(r, _authorized(request, config.DASHBOARD_KEY))


@app.get("/api/runs")
def list_runs(request: Request, limit: int = 50):
    reveal = _authorized(request, config.DASHBOARD_KEY)
    return [_run_json(r, reveal) for r in _recent(min(limit, 200))]


@app.post("/runs/{run_id}/replay")
def replay_run(run_id: str, request: Request, background: BackgroundTasks):
    if not _authorized(request, config.INTAKE_API_KEY):
        raise HTTPException(401, "Missing or invalid API key")
    try:
        new = pipeline.replay(run_id)
    except KeyError:
        raise HTTPException(404, "Run not found")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    if new.status == "received":
        background.add_task(pipeline.process, new.id)
    return {"replayed": run_id, "new_run_id": new.id, "status": new.status}


@app.get("/status", response_class=HTMLResponse)
def status_page(request: Request):
    return dashboard.render(_recent(100), _authorized(request, config.DASHBOARD_KEY))


@app.get("/health")
def health():
    runs = _recent(100)
    title, tone, detail = dashboard.health(runs)
    return {"ok": True, "automation": title, "detail": detail}


@app.get("/")
def root():
    return RedirectResponse("/status")
