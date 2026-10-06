"""Optional HTTP Basic auth (RV_AUTH_USER / RV_AUTH_PASSWORD). Off unless both are set."""
from __future__ import annotations

import base64
import logging
import secrets

from fastapi import Request
from fastapi.responses import Response

from . import config

_OPEN = {"/healthz", "/readyz"}


def enabled() -> bool:
    return bool(config.AUTH_USER and config.AUTH_PASSWORD)


def _ok(header: str | None) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        user, _, pw = base64.b64decode(header[6:].strip()).decode("utf-8").partition(":")
    except Exception:  # noqa: BLE001
        return False
    # Compare both fields every time so timing doesn't reveal which one was wrong.
    a = secrets.compare_digest(user.encode(), config.AUTH_USER.encode())
    b = secrets.compare_digest(pw.encode(), config.AUTH_PASSWORD.encode())
    return a and b


async def basic_auth(request: Request, call_next):
    path = request.url.path
    if not enabled() or path in _OPEN or (path == "/metrics" and not config.AUTH_PROTECT_METRICS):
        return await call_next(request)
    if _ok(request.headers.get("authorization")):
        return await call_next(request)
    if request.headers.get("authorization"):       # a wrong attempt, not just a first visit
        logging.getLogger("rv.auth").warning("authentication failed", extra={"fields": {
            "client": request.client.host if request.client else None, "path": path}})
    return Response("Authentication required", status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="rv-analyzer", charset="UTF-8"'})
