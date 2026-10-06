"""Settings that can be changed from the web UI (Settings page) instead of environment variables.

Precedence for every setting:  value saved in the UI  >  environment variable  >  built-in default.
Saved values live in <data dir>/settings.json (mode 600), so they survive restarts and upgrades.
Clearing a value in the UI removes the override and the environment/default applies again.

Everything here is optional: with nothing set the app runs with no AI coach, no tracing and the keyless
Wikimedia image source.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from prometheus_client import Counter

from . import config

log = logging.getLogger("rv.settings")
M_SETTINGS = Counter("rv_settings_changes_total", "Times settings were saved from the web UI")

TRUE = ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Field:
    key: str
    group: str            # ai | images | trials | tracing
    label: str
    help: str
    env: str              # environment variable that supplies the default
    kind: str = "str"     # str | url | bool | int | choice
    attr: str | None = None       # config.<attr> that the app reads (None: applied another way)
    secret: bool = False
    restart: bool = False         # only takes effect after a restart
    choices: tuple = ()
    lo: int = 0
    hi: int = 0


FIELDS: tuple[Field, ...] = (
    Field("ai_base_url", "ai", "AI server URL",
          "Open WebUI or Ollama, for example http://192.168.1.50:3000 or http://host.docker.internal:11434. Leave empty to turn the AI coach off.",
          "RV_AI_BASE_URL", "url", "AI_BASE_URL"),
    Field("ai_backend", "ai", "Server type", "Open WebUI (needs an API key) or Ollama directly (key optional).",
          "RV_AI_BACKEND", "choice", "AI_BACKEND", choices=("openwebui", "ollama")),
    Field("ai_model", "ai", "Model", "The model name exactly as the server lists it, for example llama3.1:8b.",
          "RV_AI_MODEL", "str", "AI_MODEL"),
    Field("ai_api_key", "ai", "API key", "Open WebUI: Settings > Account > API keys. Stored on this server's data volume.",
          "RV_AI_API_KEY", "str", "AI_API_KEY", secret=True),
    Field("ai_auto", "ai", "Run after every analysis", "Generate AI feedback automatically when an analysis finishes.",
          "RV_AI_AUTO", "bool", "AI_AUTO"),
    Field("target_provider", "images", "Image source",
          "auto uses Pexels when a key is set, otherwise Wikimedia Commons (no key needed).",
          "RV_TARGET_PROVIDER", "choice", "TARGET_PROVIDER", choices=("auto", "wikimedia", "pexels")),
    Field("pexels_api_key", "images", "Pexels API key", "Optional, free from pexels.com/api. Gives real stock photos.",
          "RV_PEXELS_API_KEY", "str", "PEXELS_API_KEY", secret=True),
    Field("contact", "images", "Contact (email or URL)", "Wikimedia asks API clients to identify themselves.",
          "RV_CONTACT", "str", "TARGET_CONTACT"),
    Field("judging_options", "trials", "Images in judging", "The target plus decoys: 2 is a coin flip, 4 is a 1-in-4 chance.",
          "RV_JUDGING_OPTIONS", "int", "JUDGING_OPTIONS", lo=2, hi=4),
    Field("otlp_endpoint", "tracing", "OTLP endpoint",
          "Where to send traces (Tempo: http://tempo:4318 for HTTP, or :4317 for gRPC). Empty means tracing is off.",
          "OTEL_EXPORTER_OTLP_ENDPOINT", "url", None, restart=True),
    Field("otlp_protocol", "tracing", "OTLP protocol", "http/protobuf (port 4318) or grpc (port 4317).",
          "OTEL_EXPORTER_OTLP_PROTOCOL", "choice", None, restart=True, choices=("http/protobuf", "grpc")),
)
BY_KEY = {f.key: f for f in FIELDS}
GROUPS = (("ai", "AI coach"), ("images", "Image targets"), ("trials", "Trials"), ("tracing", "Tracing (Tempo / OTLP)"))

_lock = threading.Lock()
_overrides: dict[str, object] = {}
# What the environment/defaults said before any UI override was applied.
_BASELINE: dict[str, object] = {}
_STARTUP: dict[str, object] = {}          # effective restart-only values when the process started


def _path() -> Path:
    return Path(os.getenv("RV_SETTINGS_FILE") or (config.DATA_DIR / "settings.json"))


def _baseline_of(f: Field):
    if f.attr:
        return getattr(config, f.attr)
    return os.environ.get(f.env) or (f.choices[0] if f.kind == "choice" else "")


for _f in FIELDS:
    _BASELINE[_f.key] = _baseline_of(_f)


def editable() -> bool:
    """Editing is allowed when a login is configured, or when RV_SETTINGS_EDITABLE=true is set explicitly."""
    from . import auth
    return auth.enabled() or os.getenv("RV_SETTINGS_EDITABLE", "").lower() in TRUE


def effective(key: str):
    return _overrides[key] if key in _overrides else _BASELINE[key]


def source(key: str) -> str:
    if key in _overrides:
        return "settings"
    f = BY_KEY[key]
    return "environment" if os.getenv(f.env) else "default"


# ---------------------------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------------------------
def coerce(f: Field, raw) -> object:
    """Return a clean value or raise ValueError with a message fit for the user."""
    if f.kind == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in TRUE:
            return True
        if s in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"{f.label}: choose yes or no.")
    s = str(raw).strip()
    if any(ord(c) < 32 for c in s):
        raise ValueError(f"{f.label}: contains control characters.")
    if f.kind == "int":
        try:
            n = int(s)
        except ValueError:
            raise ValueError(f"{f.label}: must be a whole number.") from None
        if not f.lo <= n <= f.hi:
            raise ValueError(f"{f.label}: must be between {f.lo} and {f.hi}.")
        return n
    if f.kind == "choice":
        s = s.lower()
        if s not in f.choices:
            raise ValueError(f"{f.label}: must be one of {', '.join(f.choices)}.")
        return s
    if f.kind == "url":
        u = urlparse(s)
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
            raise ValueError(f"{f.label}: must look like http://host:port (no username or password in it).")
        if len(s) > 300:
            raise ValueError(f"{f.label}: too long.")
        return s.rstrip("/")
    if len(s) > (500 if f.secret else 200):
        raise ValueError(f"{f.label}: too long.")
    return s


# ---------------------------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------------------------
def load() -> None:
    """Read settings.json (if any) into memory. Bad or unknown entries are skipped with a warning."""
    p = _path()
    with _lock:
        _overrides.clear()
        try:
            data = json.loads(p.read_text("utf-8")).get("values", {})
        except FileNotFoundError:
            return
        except (OSError, ValueError, AttributeError):
            log.warning("could not read settings file; ignoring it", extra={"fields": {"path": str(p)}})
            return
        for k, v in data.items():
            f = BY_KEY.get(k)
            if f is None:
                continue
            try:
                _overrides[k] = coerce(f, v)
            except ValueError as exc:
                log.warning("ignoring invalid saved setting", extra={"fields": {"key": k, "reason": str(exc)}})


def _save() -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "values": _overrides}, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)


def apply() -> None:
    """Push the effective values to where the app reads them."""
    for f in FIELDS:
        v = effective(f.key)
        if f.attr:
            setattr(config, f.attr, v)
        else:                                   # OpenTelemetry reads its own environment variables
            if v:
                os.environ[f.env] = str(v)
            else:
                os.environ.pop(f.env, None)


def startup() -> None:
    """Called once when the app starts: load, apply, and remember what needs a restart to change."""
    load()
    apply()
    _STARTUP.clear()
    _STARTUP.update({f.key: effective(f.key) for f in FIELDS if f.restart})


def restart_pending() -> list[str]:
    return [BY_KEY[k].label for k, v in _STARTUP.items() if effective(k) != v]


def update(values: dict, clear: set[str] | frozenset = frozenset()) -> tuple[list[str], list[str]]:
    """Save changes. `values` maps key -> raw text; empty text removes the override (secrets: empty keeps it).
    `clear` names secrets to remove. Returns (changed_keys, errors); nothing is saved when there are errors."""
    errors: list[str] = []
    staged: dict[str, object | None] = {}
    for k, raw in values.items():
        f = BY_KEY.get(k)
        if f is None:
            errors.append(f"Unknown setting: {k[:40]}")
            continue
        if raw is None:
            continue
        if f.secret and str(raw).strip() == "":
            continue                              # a blank secret box means "leave it alone"
        if not f.secret and f.kind != "bool" and str(raw).strip() == "":
            staged[k] = None                      # revert to the environment / default
            continue
        try:
            staged[k] = coerce(f, raw)
        except ValueError as exc:
            errors.append(str(exc))
    for k in clear:
        if k in BY_KEY and BY_KEY[k].secret:
            staged[k] = None
    if errors:
        return [], errors
    changed: list[str] = []
    with _lock:
        for k, v in staged.items():
            if v is None or v == _BASELINE[k]:     # same as the environment/default: no override needed
                if k in _overrides:
                    del _overrides[k]
                    changed.append(k)
            elif _overrides.get(k) != v:
                _overrides[k] = v
                changed.append(k)
        if changed:
            _save()
    if changed:
        apply()
        M_SETTINGS.inc()
        # Log which settings changed, never their values (some are secrets).
        log.info("settings changed", extra={"fields": {"keys": sorted(changed)}})
    return changed, []


# ---------------------------------------------------------------------------------------------
# View model for the page / API (secrets are never returned)
# ---------------------------------------------------------------------------------------------
def view() -> dict:
    groups = []
    for gkey, gtitle in GROUPS:
        items = []
        for f in FIELDS:
            if f.group != gkey:
                continue
            v = effective(f.key)
            items.append({
                "key": f.key, "label": f.label, "help": f.help, "kind": f.kind, "choices": list(f.choices),
                "secret": f.secret, "restart": f.restart, "lo": f.lo, "hi": f.hi,
                "value": "" if f.secret else ("" if v is None else v),
                "is_set": bool(v) if f.secret else None,
                "source": source(f.key),
            })
        groups.append({"key": gkey, "title": gtitle, "fields": items})
    return {"editable": editable(), "groups": groups, "restart_pending": restart_pending(),
            "file": str(_path())}
