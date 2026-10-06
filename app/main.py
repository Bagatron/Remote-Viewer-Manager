from __future__ import annotations

import json
import logging
import re
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import select, text

from . import ai_feedback, analysis, auth, config, observability, settings, rv_trials, series, settings_ui, trial_eeg, tracing
from .observability import evt, prober
from .config import DATA_DIR, MAX_UPLOAD_MB
from .db import RVSession, RVTrial, SessionLocal, engine, init_db

from .version import VERSION  # noqa: E402

LOG_FORMAT = observability.setup_logging()
log = logging.getLogger("rv")

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.globals["app_version"] = VERSION
pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="rv-worker")
# One GPU behind the LLM endpoint: generate feedback one session at a time.
ai_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rv-ai")
trial_eeg.set_ai_executor(ai_pool)

# ---- Prometheus metrics ---------------------------------------------------
M_UPLOADS = Counter("rv_uploads_total", "Files uploaded")
M_ANALYSES = Counter("rv_analyses_total", "Analyses finished", ["status"])
M_DURATION = Histogram("rv_analysis_duration_seconds", "Analysis wall time")
M_SESSIONS = Gauge("rv_sessions", "Stored sessions", ["status"])
M_AI = Counter("rv_ai_feedback_total", "AI feedback generations finished", ["status"])
M_ANALYSES_RUNNING = Gauge("rv_analyses_in_progress", "Analyses running right now")
M_AI_RUNNING = Gauge("rv_ai_feedback_in_progress", "AI feedback generations running right now")
M_TRIAL_COUNT = Gauge("rv_trials_stored", "Stored RV trials", ["status"])
M_AI_DURATION = Histogram(
    "rv_ai_feedback_duration_seconds", "AI feedback wall time",
    buckets=(5, 15, 30, 60, 120, 240, 480, 900),
)

ALLOWED_EXT = {".csv", ".xlsx", ".xlsm"}
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def _process(session_id: str) -> None:
    """Run analysis for one session (called from the worker pool)."""
    with SessionLocal() as db:
        s = db.get(RVSession, session_id)
        if s is None:
            return
        params = analysis.Params(**s.params)
        start = time.perf_counter()
        M_ANALYSES_RUNNING.inc()
        evt(log, logging.INFO, "analysis started", session_id=session_id)
        try:
            with tracing.span("analysis.run", session_id=session_id):
                metrics = analysis.run_and_store(s.dir / s.stored_filename, s.dir, params, s.name)
            s.metrics_json = json.dumps(metrics)
            s.status, s.error = "done", None
            M_ANALYSES.labels("done").inc()
            evt(log, logging.INFO, "analysis finished", session_id=session_id, rows=metrics.get("rows"),
                duration_s=round(time.perf_counter() - start, 2))
        except Exception as exc:  # noqa: BLE001 - surfaced to the user in the UI
            log.exception("analysis failed for %s", session_id, extra={"fields": {"session_id": session_id}})
            s.status, s.error = "failed", str(exc)
            M_ANALYSES.labels("failed").inc()
        finally:
            M_ANALYSES_RUNNING.dec()
            M_DURATION.observe(time.perf_counter() - start)
        db.commit()
        done, sdir = s.status == "done", s.dir
    if done and config.AI_AUTO and ai_feedback.enabled():
        _queue_ai(session_id, sdir)


def _queue_ai(session_id: str, sdir: Path, focus: str = "") -> None:
    ai_feedback.mark_pending(sdir, focus)
    ai_pool.submit(tracing.bind(_run_ai), session_id)


def _run_ai(session_id: str) -> None:
    """Generate AI feedback for one session (called from the AI worker)."""
    with SessionLocal() as db:
        s = db.get(RVSession, session_id)
        if s is None:
            return
        earlier = list(db.scalars(
            select(RVSession)
            .where(RVSession.status == "done", RVSession.id != s.id, RVSession.created_at <= s.created_at)
            .order_by(RVSession.created_at)
        ))
        history = ai_feedback.history_from_sessions(earlier)
        notes, sdir = s.notes, s.dir
    M_AI_RUNNING.inc()
    try:
        with tracing.span("ai.feedback", session_id=session_id, history_sessions=len(history)):
            st = ai_feedback.generate(sdir, notes, history)
    finally:
        M_AI_RUNNING.dec()
    M_AI.labels(st.get("status", "failed")).inc()
    evt(log, logging.INFO if st.get("status") == "done" else logging.WARNING, "ai feedback finished",
        session_id=session_id, status=st.get("status"), duration_s=st.get("duration_s"), error=st.get("error"))
    if st.get("duration_s") is not None:
        M_AI_DURATION.observe(st["duration_s"])


def _refresh_gauges() -> None:
    with SessionLocal() as db:
        counts = {"pending": 0, "done": 0, "failed": 0}
        for (st,) in db.execute(select(RVSession.status)):
            counts[st] = counts.get(st, 0) + 1
    for k, v in counts.items():
        M_SESSIONS.labels(k).set(v)
    with SessionLocal() as db:
        tc: dict[str, int] = {"assigned": 0, "judging": 0, "revealed": 0, "complete": 0, "abandoned": 0}
        for (st,) in db.execute(select(RVTrial.status)):
            tc[st] = tc.get(st, 0) + 1
    for k, v in tc.items():
        M_TRIAL_COUNT.labels(k).set(v)
    observability.refresh_static_gauges()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    from . import config as _cfg, targets as _targets
    evt(log, logging.INFO, "starting", version=VERSION, log_format=LOG_FORMAT, data_dir=str(DATA_DIR),
        ai_enabled=ai_feedback.enabled(), ai_backend=_cfg.AI_BACKEND if ai_feedback.enabled() else None,
        auth_enabled=auth.enabled(), target_providers=_targets._providers(), probes=_cfg.PROBES_ENABLED)
    prober.register_executors(analysis=pool, ai=ai_pool)
    observability.init_probes()
    prober.start()
    # Resume anything that was queued/in-flight when the pod last stopped.
    with SessionLocal() as db:
        for s in db.scalars(select(RVSession).where(RVSession.status == "pending")):
            pool.submit(tracing.bind(_process), s.id)
        # ...and AI feedback that was queued/running when the pod stopped.
        for s in db.scalars(select(RVSession).where(RVSession.status == "done")):
            if ai_feedback.is_pending(s.dir):
                ai_pool.submit(tracing.bind(_run_ai), s.id)
        trial_eeg.resume(list(db.scalars(select(RVTrial))))
    yield
    prober.stop()
    evt(log, logging.INFO, "stopping")
    tracing.shutdown()
    pool.shutdown(wait=False, cancel_futures=True)
    ai_pool.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="RV Analyzer", version=VERSION, lifespan=lifespan)
app.middleware("http")(auth.basic_auth)
app.middleware("http")(observability.request_middleware)   # added last = outermost: also counts 401s
settings.startup()                                         # saved UI settings override env; before tracing
tracing.setup(app, engine=engine)                       # no-op unless OTEL_EXPORTER_OTLP_ENDPOINT is set
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
app.include_router(rv_trials.router)
app.include_router(settings_ui.router)


# ---- health / metrics -------------------------------------------------------
@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        if not DATA_DIR.is_dir():
            raise RuntimeError("data dir missing")
        return {"status": "ready"}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"status": "not ready", "error": str(exc)}, status_code=503)


@app.get("/api/status")
def api_status():
    """Component health as JSON (the same data as rv_component_up), for humans and scripts."""
    return {"version": VERSION, **prober.snapshot(), "ai_enabled": ai_feedback.enabled()}


@app.post("/api/client-event")
async def client_event(request: Request):
    """The browser reports Bluetooth capture outcomes so the server can count and log them."""
    raw = await request.body()
    if len(raw) > 2048:
        raise HTTPException(413, "Too large")
    try:
        body = json.loads(raw or b"{}")
        event = str(body.get("event", ""))
        detail = str(body.get("detail", ""))[:200]
    except (ValueError, AttributeError):
        raise HTTPException(400, "Expected JSON")
    if event not in observability.CLIENT_EVENTS:
        raise HTTPException(400, "Unknown event")
    observability.M_CLIENT.labels(event).inc()
    bad = event in ("bt_connect_failed", "bt_unavailable", "bt_disconnected", "bt_reconnect_failed")
    evt(logging.getLogger("rv.client"), logging.WARNING if bad else logging.INFO, event, detail=detail,
        user_agent=request.headers.get("user-agent", "")[:140])
    return Response(status_code=204)


@app.get("/metrics")
def metrics():
    _refresh_gauges()
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---- pages ------------------------------------------------------------------
def _fmt_params(window: int, top_n: int, min_gap: int) -> dict:
    return analysis.Params(
        window=max(1, window), top_n=max(1, min(top_n, 20)), min_gap=max(0, min_gap)
    ).to_dict()


@app.get("/guide")
def guide(request: Request):
    from . import config as _c
    n = _c.JUDGING_OPTIONS
    return templates.TemplateResponse(request, "guide.html", {
        "n_opts": n, "n_word": {2: "two", 3: "three", 4: "four"}[n], "chance_pct": round(100 / n)})


def _trend_entries() -> list[dict]:
    """Every finished recording, oldest first: uploaded sessions plus EEG captured inside RV trials."""
    out: list[dict] = []
    with SessionLocal() as db:
        for s in db.scalars(select(RVSession).where(RVSession.status == "done")):
            out.append({"name": s.name, "rv_raw_mean": s.metrics.get("rv_raw_mean", 0.0), "kind": "session",
                        "when": s.created_at})
        for t in db.scalars(select(RVTrial)):
            st = trial_eeg.status(t.dir)
            if not st or st.get("status") != "done":
                continue
            try:
                m = json.loads((trial_eeg.out_dir(t.dir) / "metrics.json").read_text())
                out.append({"name": f"trial {t.coordinate}", "rv_raw_mean": float(m["rv_raw_mean"]), "kind": "trial",
                            "when": t.created_at})
            except (OSError, ValueError, KeyError, TypeError):
                continue
    out.sort(key=lambda e: e["when"].timestamp() if e["when"].tzinfo else e["when"].replace(tzinfo=timezone.utc).timestamp())
    return out


@app.get("/")
def index(request: Request):
    with SessionLocal() as db:
        sessions = list(db.scalars(select(RVSession).order_by(RVSession.created_at.desc())))
    entries = _trend_entries()
    n_trials = sum(1 for e in entries if e["kind"] == "trial")
    trend_ready = len(entries) > 0
    pending = any(s.status == "pending" for s in sessions)
    return templates.TemplateResponse(
        request, "index.html",
        {"sessions": sessions, "trend_ready": trend_ready, "pending": pending, "max_mb": MAX_UPLOAD_MB,
         "n_trend": len(entries), "n_trend_trials": n_trials, "n_trend_sessions": len(entries) - n_trials},
    )


@app.post("/upload")
async def upload(
    files: list[UploadFile] = File(...),
    window: int = Form(7),
    top_n: int = Form(3),
    min_gap: int = Form(20),
):
    params = _fmt_params(window, top_n, min_gap)
    created = []
    for f in files:
        ext = Path(f.filename or "").suffix.lower()
        if ext not in ALLOWED_EXT:
            raise HTTPException(400, f"'{f.filename}': unsupported type. Upload .csv or .xlsx.")
        s = RVSession(
            id=uuid.uuid4().hex,
            name=Path(f.filename).stem[:200],
            original_filename=(f.filename or "upload")[:255],
            stored_filename=f"original{ext}",
            params_json=json.dumps(params),
        )
        s.dir.mkdir(parents=True, exist_ok=True)
        dest = s.dir / s.stored_filename
        size, limit = 0, MAX_UPLOAD_MB * 1024 * 1024
        with dest.open("wb") as out:
            while chunk := await f.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    out.close()
                    shutil.rmtree(s.dir, ignore_errors=True)
                    raise HTTPException(413, f"'{f.filename}' exceeds {MAX_UPLOAD_MB} MB limit.")
                out.write(chunk)
        with SessionLocal() as db:
            db.add(s)
            db.commit()
        M_UPLOADS.inc()
        pool.submit(tracing.bind(_process), s.id)
        created.append(s.id)
    if len(created) == 1:
        return RedirectResponse(f"/sessions/{created[0]}", status_code=303)
    return RedirectResponse("/", status_code=303)


def _get_or_404(db, session_id: str) -> RVSession:
    s = db.get(RVSession, session_id)
    if s is None:
        raise HTTPException(404, "Session not found")
    return s


@app.get("/sessions/{session_id}")
def session_detail(request: Request, session_id: str):
    with SessionLocal() as db:
        s = _get_or_404(db, session_id)
    files = sorted(p.name for p in s.dir.iterdir()) if s.dir.exists() else []
    windows = []
    wcsv = s.dir / "intuitive_windows.csv"
    if wcsv.exists():
        import pandas as pd

        windows = pd.read_csv(wcsv).round(3).to_dict("records")
    summary = ""
    if (s.dir / "coaching_summary.txt").exists():
        summary = (s.dir / "coaching_summary.txt").read_text()
    ai_status = ai_feedback.read_status(s.dir)
    ai = {
        "enabled": ai_feedback.enabled(),
        "status": ai_status,
        "html": ai_feedback.render_markdown(ai_feedback.read_feedback(s.dir)),
    }
    return templates.TemplateResponse(
        request, "session.html",
        {"s": s, "files": files, "windows": windows, "summary": summary,
         "has_scatter": "rvindex_feedback_scatter.png" in files,
         "ai": ai, "tips": analysis.COACHING_TIPS},
    )


@app.get("/sessions/{session_id}/files/{filename}")
def session_file(session_id: str, filename: str, download: bool = False):
    if not SAFE_NAME.match(filename):
        raise HTTPException(400, "Bad filename")
    with SessionLocal() as db:
        s = _get_or_404(db, session_id)
    path = s.dir / filename
    if not path.is_file():
        raise HTTPException(404, "File not found")
    name = s.original_filename if filename == s.stored_filename else f"{s.name}_{filename}"
    return FileResponse(path, filename=name if download else None)


@app.post("/sessions/{session_id}/reanalyze")
def reanalyze(
    session_id: str,
    window: int = Form(7),
    top_n: int = Form(3),
    min_gap: int = Form(20),
):
    with SessionLocal() as db:
        s = _get_or_404(db, session_id)
        s.params_json = json.dumps(_fmt_params(window, top_n, min_gap))
        s.status, s.error = "pending", None
        db.commit()
    pool.submit(tracing.bind(_process), session_id)
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@app.post("/sessions/{session_id}/ai-feedback")
def ai_feedback_run(session_id: str, focus: str = Form("")):
    with SessionLocal() as db:
        s = _get_or_404(db, session_id)
    if s.status != "done":
        raise HTTPException(409, "Wait for the analysis to finish first.")
    if not ai_feedback.enabled():
        raise HTTPException(503, "AI feedback is not configured (RV_AI_BASE_URL / RV_AI_MODEL).")
    if not ai_feedback.is_pending(s.dir):
        _queue_ai(session_id, s.dir, focus)
    return RedirectResponse(f"/sessions/{session_id}#ai", status_code=303)


@app.post("/sessions/{session_id}/notes")
def save_notes(session_id: str, notes: str = Form("")):
    with SessionLocal() as db:
        s = _get_or_404(db, session_id)
        s.notes = notes[:20000]
        db.commit()
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@app.post("/sessions/{session_id}/delete")
def delete_session(session_id: str):
    with SessionLocal() as db:
        s = _get_or_404(db, session_id)
        shutil.rmtree(s.dir, ignore_errors=True)
        db.delete(s)
        db.commit()
    return RedirectResponse("/", status_code=303)


@app.get("/trend.png")
def trend_png():
    entries = _trend_entries()
    png = analysis.plot_trend([{k: e[k] for k in ("name", "rv_raw_mean", "kind")} for e in entries])
    if png is None:
        raise HTTPException(404, "No completed recordings yet")
    (DATA_DIR / "trend_rvindex.png").write_bytes(png)  # keep a stored copy too
    return Response(png, media_type="image/png")


# ---- JSON API ---------------------------------------------------------------
@app.get("/api/ai/health")
def api_ai_health():
    """Is the LLM endpoint reachable, and is the configured model installed there?"""
    return ai_feedback.health()


@app.get("/api/sessions/{session_id}/ai")
def api_session_ai(session_id: str):
    with SessionLocal() as db:
        s = _get_or_404(db, session_id)
    return {"status": ai_feedback.read_status(s.dir), "feedback_markdown": ai_feedback.read_feedback(s.dir)}


@app.get("/api/sessions/{session_id}/series")
def api_session_series(session_id: str):
    with SessionLocal() as db:
        s = _get_or_404(db, session_id)
    data = series.build_series(s.dir) if s.status == "done" else None
    return {"series": data, "status": s.status}


@app.get("/api/sessions")
def api_sessions():
    with SessionLocal() as db:
        rows = list(db.scalars(select(RVSession).order_by(RVSession.created_at.desc())))
    return [
        {"id": s.id, "name": s.name, "status": s.status, "created_at": s.created_at.isoformat(),
         "metrics": s.metrics, "params": s.params, "error": s.error}
        for s in rows
    ]
