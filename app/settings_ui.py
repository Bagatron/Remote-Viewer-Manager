"""Settings page and its small JSON API."""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from . import ai_feedback, auth, observability, settings, targets, tracing
from .observability import prober
from .version import VERSION

log = logging.getLogger("rv.settings")
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["app_version"] = VERSION

MAX_BODY = 16 * 1024
COMPONENT_NOTES = {
    "database": "required", "data_volume": "required", "workers": "required",
    "ai_backend": "AI server reachable", "ai_model": "chosen model present", "image_provider": "can download a target image",
}


def _recheck() -> None:
    """After a change, re-run the checks that depend on it (in the background: they make network calls)."""
    def run():
        observability.refresh_static_gauges()
        prober.run_once(only=("ai", "image_provider"), force=True)
    threading.Thread(target=run, daemon=True, name="settings-recheck").start()


@router.get("/settings")
def settings_page(request: Request):
    snap = prober.snapshot()
    comps = [{"name": k, "note": COMPONENT_NOTES.get(k, ""), **v} for k, v in sorted(snap["components"].items())]
    return templates.TemplateResponse(request, "settings.html", {
        "view": settings.view(), "components": comps, "ready": snap["ready"],
        "tracing_on": tracing.enabled(), "ai_on": ai_feedback.enabled(),
        "auth_on": auth.enabled(),
    })


@router.get("/api/settings")
def api_settings():
    """Current effective settings with their source. Secret values are never returned, only whether they are set."""
    return settings.view()


@router.post("/api/settings")
async def api_settings_save(request: Request):
    if "application/json" not in request.headers.get("content-type", ""):
        return JSONResponse({"errors": ["Send JSON (Content-Type: application/json)."]}, status_code=415)
    if not settings.editable():
        return JSONResponse({"errors": ["Settings are read-only here. Set RV_AUTH_USER and RV_AUTH_PASSWORD, "
                                        "or RV_SETTINGS_EDITABLE=true, to allow editing."]}, status_code=403)
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return JSONResponse({"errors": ["Request too large."]}, status_code=413)
    try:
        body = json.loads(raw)
        values, clear = body.get("values", {}), set(body.get("clear", []))
        if not isinstance(values, dict):
            raise ValueError
    except (ValueError, AttributeError, TypeError):
        return JSONResponse({"errors": ["Invalid JSON."]}, status_code=400)
    changed, errors = settings.update(values, clear)
    if errors:
        return JSONResponse({"errors": errors}, status_code=400)
    if changed:
        _recheck()
    return {"changed": changed, "restart_pending": settings.restart_pending()}


@router.post("/api/settings/test")
async def api_settings_test(request: Request):
    """Try the saved configuration. Never returns keys, and never the hidden image."""
    if "application/json" not in request.headers.get("content-type", ""):
        return JSONResponse({"errors": ["Send JSON (Content-Type: application/json)."]}, status_code=415)
    try:
        what = (json.loads(await request.body()) or {}).get("what")
    except (ValueError, AttributeError):
        what = None
    if what == "ai":
        h = ai_feedback.health()
        if h.get("error") and h.get("base_url") and "Could not reach" not in h["error"] and h["error"].startswith(("timed out", "All connection", "[Errno")):
            h["error"] = f"Could not reach {h['base_url']}: {h['error']}. Check the address, and that this container can reach it (inside Docker, localhost means the container itself; use host.docker.internal)."
        return {"what": "ai", "ok": bool(h.get("ok") and h.get("model_available")), "enabled": h["enabled"],
                "error": h.get("error") or (None if h.get("model_available") or not h.get("ok")
                                            else f"Connected, but model {h.get('model')!r} is not on the server."),
                "models": h.get("models", [])[:50]}
    if what == "images":
        r = targets.probe()
        return {"what": "images", "ok": bool(r.get("ok")), "error": r.get("error"), "provider": r.get("provider_used"),
                "providers": r.get("providers")}
    return JSONResponse({"errors": ["what must be 'ai' or 'images'."]}, status_code=400)
