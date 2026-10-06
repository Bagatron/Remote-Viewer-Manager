"""Logging, request metrics and component health probes.

* setup_logging():     one handler on stdout, JSON lines (Loki) or plain text.
* request_middleware:  request id, access log line and rv_http_* metrics per route template.
* prober:              background checks behind rv_component_up{component=...} (1 = working, 0 = broken).

Nothing here logs request bodies, query strings, credentials or anything about a trial's target.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Callable

from fastapi import Request
from prometheus_client import Counter, Gauge, Histogram
from sqlalchemy import text
from starlette.routing import Match

from . import config, tracing
from .version import VERSION

log = logging.getLogger("rv.obs")
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
def evt(logger: logging.Logger, level: int, msg: str, **fields) -> None:
    """Log a message with structured key=value fields."""
    logger.log(level, msg, extra={"fields": fields})


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname, "logger": record.name, "msg": record.getMessage(),
        }
        rid = request_id_var.get()
        if rid != "-":
            d["request_id"] = rid
        ids = tracing.current_ids()
        if ids:
            d["trace_id"], d["span_id"] = ids
        for k, v in (getattr(record, "fields", None) or {}).items():
            d.setdefault(k, v)
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return json.dumps(d, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        out = super().format(record)
        extra = " ".join(f"{k}={v}" for k, v in (getattr(record, "fields", None) or {}).items())
        rid = request_id_var.get()
        ids = tracing.current_ids()
        return (out + (f" {extra}" if extra else "") + (f" request_id={rid}" if rid != "-" else "")
                + (f" trace_id={ids[0]}" if ids else ""))


def setup_logging() -> str:
    fmt = config.LOG_FORMAT
    if fmt == "auto":
        fmt = "json" if os.getenv("KUBERNETES_SERVICE_HOST") else "text"
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    keep = [h for h in root.handlers if h.__class__.__module__.startswith("_pytest")]   # pytest's capture
    root.handlers[:] = keep + [handler]
    root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
    for name in ("uvicorn", "uvicorn.error"):          # same format as everything else
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    logging.getLogger("uvicorn.access").disabled = True   # request_middleware writes the access line
    # httpx logs every request URL at INFO, and the image-search URL contains the hidden target's theme.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return fmt


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------
M_INFO = Gauge("rv_info", "Build information", ["version"])
M_INFO.labels(VERSION).set(1)
M_COMPONENT_UP = Gauge("rv_component_up", "1 when a component this app depends on is working, 0 when it is not", ["component"])
M_COMPONENT_DURATION = Gauge("rv_component_check_duration_seconds", "How long the last check of a component took", ["component"])
M_COMPONENT_LAST = Gauge("rv_component_last_check_timestamp_seconds", "When a component was last checked", ["component"])
M_COMPONENT_OK = Gauge("rv_component_last_success_timestamp_seconds", "When a component was last seen working", ["component"])
M_READY = Gauge("rv_ready", "1 when every required component (database, data_volume, workers) is up")
M_AI_ENABLED = Gauge("rv_ai_enabled", "1 when the AI coach is configured")
M_VOL_FREE = Gauge("rv_data_volume_free_bytes", "Free bytes on the data volume")
M_VOL_TOTAL = Gauge("rv_data_volume_total_bytes", "Size of the data volume in bytes")
M_QUEUE = Gauge("rv_worker_queue_depth", "Jobs waiting for a worker", ["pool"])
M_HTTP = Counter("rv_http_requests_total", "HTTP requests", ["method", "route", "status"])
M_HTTP_DURATION = Histogram(
    "rv_http_request_duration_seconds", "HTTP request time until the response starts", ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
M_HTTP_INFLIGHT = Gauge("rv_http_requests_in_progress", "HTTP requests being handled right now")
M_CLIENT = Counter("rv_client_events_total", "Events reported by the browser (Bluetooth capture)", ["event"])

REQUIRED = ("database", "data_volume", "workers")
CLIENT_EVENTS = frozenset({
    "bt_unavailable", "bt_connect_retry", "bt_connected", "bt_connect_failed", "bt_disconnected",
    "bt_reconnected", "bt_reconnect_failed",
    "capture_started", "capture_stopped",
})


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------
_RID_OK = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_QUIET = {"/healthz", "/readyz", "/metrics"}
req_log = logging.getLogger("rv.http")


def _route_label(request: Request) -> str:
    """Route template ('/rv/trials/{trial_id}'), never the concrete path, so labels stay few."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if path is None:
        for r in request.app.router.routes:
            m, _ = r.matches(request.scope)
            if m == Match.FULL:
                path = getattr(r, "path", None)
                break
    if path is None:
        return "/static" if request.url.path.startswith("/static") else "unmatched"
    return "/static" if path.startswith("/static") else path


async def request_middleware(request: Request, call_next):
    rid = request.headers.get("x-request-id", "")
    if not _RID_OK.match(rid):
        rid = uuid.uuid4().hex[:16]
    token = request_id_var.set(rid)
    start, status = time.perf_counter(), 500
    M_HTTP_INFLIGHT.inc()
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = rid
        ids = tracing.current_ids()
        if ids:
            response.headers["X-Trace-ID"] = ids[0]      # paste into Grafana > Tempo to open the trace
        return response
    except Exception:
        req_log.exception("unhandled error", extra={"fields": {"method": request.method, "path": request.url.path}})
        raise
    finally:
        dur = time.perf_counter() - start
        M_HTTP_INFLIGHT.dec()
        route = _route_label(request)
        M_HTTP.labels(request.method, route, str(status)).inc()
        M_HTTP_DURATION.labels(request.method, route).observe(dur)
        path = request.url.path
        quiet = path in _QUIET or route == "/static"
        level = logging.DEBUG if quiet and status < 500 else logging.WARNING if status >= 500 else logging.INFO
        evt(req_log, level, "request", method=request.method, path=path, route=route, status=status,
            duration_ms=round(dur * 1000, 1), client=request.client.host if request.client else None)
        request_id_var.reset(token)


# --------------------------------------------------------------------------
# Component probes
# --------------------------------------------------------------------------
Result = dict  # component -> (ok: bool, detail: str | None)


class Check:
    def __init__(self, name: str, fn: Callable[[], Result], components: tuple[str, ...], interval: Callable[[], int],
                 enabled: Callable[[], bool] = lambda: True):
        self.name, self.fn, self.components, self.interval, self.enabled = name, fn, components, interval, enabled
        self.next_due = 0.0


class Prober:
    def __init__(self) -> None:
        self.checks: list[Check] = []
        self.state: dict[str, dict] = {}
        self.executors: dict[str, object] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- registration
    def add(self, *a, **kw) -> None:
        self.checks.append(Check(*a, **kw))

    def register_executors(self, **executors) -> None:
        self.executors.update(executors)

    # ---- running
    def run_check(self, c: Check) -> None:
        t0 = time.perf_counter()
        try:
            if c.name in REQUIRED:
                with tracing.quiet():                 # frequent and uninteresting: keep them out of the traces
                    results = c.fn()
            else:
                with tracing.span(f"probe.{c.name}"):
                    results = c.fn()
        except Exception as exc:  # noqa: BLE001 - a failing probe is a result, not a crash
            results = {comp: (False, f"{type(exc).__name__}: {exc}"[:300]) for comp in c.components}
        dur, now = time.perf_counter() - t0, time.time()
        for comp, (ok, detail) in results.items():
            with self._lock:
                prev = self.state.get(comp)
                self.state[comp] = {
                    "up": bool(ok), "detail": detail, "checked_at": now, "duration_s": round(dur, 3),
                    "last_ok": now if ok else (prev or {}).get("last_ok"),
                    "since": now if prev is None or prev["up"] != bool(ok) else prev["since"],
                }
            M_COMPONENT_UP.labels(comp).set(1 if ok else 0)
            M_COMPONENT_DURATION.labels(comp).set(dur)
            M_COMPONENT_LAST.labels(comp).set(now)
            if ok:
                M_COMPONENT_OK.labels(comp).set(now)
            if prev is None or prev["up"] != bool(ok):
                evt(log, logging.INFO if ok else logging.WARNING, f"component {'up' if ok else 'DOWN'}",
                    component=comp, detail=detail)
        M_READY.set(1 if self.ready() else 0)

    def run_once(self, only: tuple[str, ...] | None = None, force: bool = True) -> None:
        now = time.monotonic()
        for c in self.checks:
            if only is not None and c.name not in only:
                continue
            if not c.enabled():
                for comp in c.components:          # not configured: no series rather than a fake 1
                    self._forget(comp)
                continue
            if force or now >= c.next_due:
                self.run_check(c)
                c.next_due = time.monotonic() + max(1, c.interval())

    def _forget(self, comp: str) -> None:
        with self._lock:
            self.state.pop(comp, None)
        for g in (M_COMPONENT_UP, M_COMPONENT_DURATION, M_COMPONENT_LAST, M_COMPONENT_OK):
            try:
                g.remove(comp)
            except KeyError:
                pass

    def ready(self) -> bool:
        with self._lock:
            return all(self.state.get(c, {}).get("up") for c in REQUIRED)

    def snapshot(self) -> dict:
        with self._lock:
            comps = {k: dict(v) for k, v in self.state.items()}
        return {"ready": all(comps.get(c, {}).get("up") for c in REQUIRED), "components": comps}

    # ---- thread
    def start(self) -> None:
        if not config.PROBES_ENABLED or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="rv-prober", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once(force=False)
            except Exception:  # noqa: BLE001
                log.exception("prober loop error")
            self._stop.wait(1.0)


prober = Prober()


def _check_database() -> Result:
    from .db import SessionLocal
    with SessionLocal() as db:
        db.execute(text("SELECT 1"))
    return {"database": (True, None)}


def _check_data_volume() -> Result:
    d = config.DATA_DIR
    probe = d / ".rv-probe"
    token = uuid.uuid4().bytes
    with open(probe, "wb") as f:               # prove the volume is writable, not just mounted
        f.write(token)
        f.flush()
        os.fsync(f.fileno())
    ok = probe.read_bytes() == token
    probe.unlink()
    u = shutil.disk_usage(d)
    M_VOL_FREE.set(u.free)
    M_VOL_TOTAL.set(u.total)
    return {"data_volume": (ok, f"{u.free / 1e9:.1f} GB free of {u.total / 1e9:.1f} GB" if ok else "read-back mismatch")}


def _check_workers() -> Result:
    bad = []
    for name, ex in prober.executors.items():
        if getattr(ex, "_shutdown", False):
            bad.append(name)
        q = getattr(ex, "_work_queue", None)
        if q is not None:
            M_QUEUE.labels(name).set(q.qsize())
    return {"workers": (not bad, ("stopped: " + ", ".join(bad)) if bad else None)}


def _check_ai() -> Result:
    from . import ai_feedback
    h = ai_feedback.health()
    up = bool(h.get("ok"))
    model_ok = up and bool(h.get("model_available"))
    return {"ai_backend": (up, h.get("error")),
            "ai_model": (model_ok, None if model_ok else (f"model {config.AI_MODEL!r} not found on the backend" if up else "backend unreachable"))}


def _check_image_provider() -> Result:
    from . import targets
    r = targets.probe()
    return {"image_provider": (bool(r.get("ok")), r.get("error") or f"via {r.get('provider_used')}")}


def init_probes() -> None:
    """Register the standard checks (idempotent) and run the instant ones so /metrics is complete from the first scrape."""
    if not prober.checks:
        prober.add("database", _check_database, ("database",), lambda: config.PROBE_INTERVAL_S)
        prober.add("data_volume", _check_data_volume, ("data_volume",), lambda: config.PROBE_INTERVAL_S)
        prober.add("workers", _check_workers, ("workers",), lambda: config.PROBE_INTERVAL_S)
        from . import ai_feedback
        prober.add("ai", _check_ai, ("ai_backend", "ai_model"), lambda: config.PROBE_AI_INTERVAL_S, ai_feedback.enabled)
        prober.add("image_provider", _check_image_provider, ("image_provider",), lambda: config.PROBE_TARGET_INTERVAL_S)
    prober.run_once(only=REQUIRED)


def refresh_static_gauges() -> None:
    from . import ai_feedback
    M_AI_ENABLED.set(1 if ai_feedback.enabled() else 0)
