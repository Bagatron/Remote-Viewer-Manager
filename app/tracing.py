"""Optional OpenTelemetry tracing (for Tempo or any OTLP backend).

Off unless an OTLP endpoint is configured with the standard variables:
    OTEL_EXPORTER_OTLP_ENDPOINT=http://tempo:4318                (HTTP) or http://tempo:4317 with
    OTEL_EXPORTER_OTLP_PROTOCOL=grpc                             (gRPC; default is http/protobuf)
Also honoured: OTEL_SERVICE_NAME, OTEL_RESOURCE_ATTRIBUTES, OTEL_TRACES_SAMPLER(_ARG), OTEL_EXPORTER_OTLP_HEADERS,
OTEL_SDK_DISABLED=true to force it off.

What is traced: every HTTP request (except health, metrics and static files), outgoing HTTP calls (Open WebUI,
Wikimedia, Pexels), database queries, and named spans around the slow work (analysis, AI feedback, target preparation).
Query strings are removed from every URL on every span: the image-search URL contains the hidden target's theme.
"""
from __future__ import annotations

import contextlib
import functools
import logging
import os
from typing import Callable

log = logging.getLogger("rv.tracing")

_enabled = False
_provider = None
_tracer = None


def enabled() -> bool:
    return _enabled


def configured() -> bool:
    if os.getenv("OTEL_SDK_DISABLED", "").lower() in ("1", "true", "yes"):
        return False
    return bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"))


# --------------------------------------------------------------------------
# Scrubbing: no URL on any span may carry a query string
# --------------------------------------------------------------------------
_URL_KEYS = ("http.url", "url.full")
_TARGET_KEYS = ("http.target",)


def _scrub_span(span) -> None:
    attrs = getattr(span, "attributes", None) or {}
    for k in _URL_KEYS + _TARGET_KEYS:
        v = attrs.get(k)
        if isinstance(v, str) and ("?" in v or "#" in v):
            span.set_attribute(k, v.split("?", 1)[0].split("#", 1)[0])
    if "url.query" in attrs:
        span.set_attribute("url.query", "")


def _server_hook(span, scope) -> None:
    _scrub_span(span)


def _client_hook(span, request) -> None:
    url = str(getattr(request, "url", "")).split("?", 1)[0].split("#", 1)[0]
    if url:
        for k in _URL_KEYS:
            span.set_attribute(k, url)
    _scrub_span(span)


async def _client_hook_async(span, request) -> None:
    _client_hook(span, request)


# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------
def _make_exporter():
    proto = (os.getenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL") or os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL") or "http/protobuf").lower()
    if proto == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    return OTLPSpanExporter()


def setup(app, engine=None, exporter=None) -> bool:
    """Turn tracing on when configured (or when a test passes an exporter). Returns whether it is on."""
    global _enabled, _provider, _tracer
    if _enabled:
        return True
    if exporter is None and not configured():
        return False
    # Traces only: Prometheus already carries the metrics, and Tempo has no /v1/metrics endpoint to receive them.
    os.environ.setdefault("OTEL_METRICS_EXPORTER", "none")
    os.environ.setdefault("OTEL_LOGS_EXPORTER", "none")
    try:
        from opentelemetry import trace
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning("tracing is configured but the OpenTelemetry packages are not installed; tracing stays off")
        return False
    from .version import VERSION
    try:
        resource = Resource.create({
            "service.name": os.getenv("OTEL_SERVICE_NAME", "rv-analyzer"),
            "service.version": VERSION,
            "service.instance.id": os.getenv("HOSTNAME", "local"),
        })                                       # Resource.create also reads OTEL_RESOURCE_ATTRIBUTES
        provider = TracerProvider(resource=resource)     # sampler comes from OTEL_TRACES_SAMPLER (default: parent-based, always on)
        provider.add_span_processor(BatchSpanProcessor(exporter or _make_exporter()))
        trace.set_tracer_provider(provider)
        FastAPIInstrumentor.instrument_app(
            app, tracer_provider=provider, server_request_hook=_server_hook,
            excluded_urls="/healthz,/readyz,/metrics,/static/", exclude_spans=["receive", "send"])
        HTTPXClientInstrumentor().instrument(
            tracer_provider=provider, request_hook=_client_hook, async_request_hook=_client_hook_async)
        if engine is not None:
            from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
            SQLAlchemyInstrumentor().instrument(engine=engine, tracer_provider=provider)
    except Exception:  # noqa: BLE001 - observability must never stop the app from starting
        log.exception("could not start tracing; continuing without it")
        return False
    _provider, _tracer, _enabled = provider, provider.get_tracer("rv-analyzer"), True
    log.info("tracing enabled", extra={"fields": {
        "endpoint": os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or "(test exporter)",
        "protocol": os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")}})
    return True


def shutdown() -> None:
    """Flush pending spans (called on shutdown)."""
    if _provider is not None:
        try:
            _provider.shutdown()
        except Exception:  # noqa: BLE001
            log.exception("tracing shutdown failed")


# --------------------------------------------------------------------------
# Helpers used by the app (all no-ops when tracing is off)
# --------------------------------------------------------------------------
def span(name: str, **attrs):
    """`with tracing.span("analysis.run", session_id=...) as sp:`; sp is None when tracing is off."""
    if not _enabled:
        return contextlib.nullcontext()
    return _tracer.start_as_current_span(name, attributes={k: v for k, v in attrs.items() if v is not None})


def set_attrs(sp, **attrs) -> None:
    if sp is not None:
        for k, v in attrs.items():
            if v is not None:
                sp.set_attribute(k, v)


def quiet():
    """Suppress automatic spans inside the block (used for the app's own internal health checks)."""
    if not _enabled:
        return contextlib.nullcontext()
    from opentelemetry.instrumentation.utils import suppress_instrumentation
    return suppress_instrumentation()


def bind(fn: Callable) -> Callable:
    """Make a function run, in another thread, as a child of the span that is current right now."""
    if not _enabled:
        return fn
    from opentelemetry import context as otel_context
    ctx = otel_context.get_current()

    @functools.wraps(fn)
    def run(*a, **kw):
        token = otel_context.attach(ctx)
        try:
            return fn(*a, **kw)
        finally:
            otel_context.detach(token)
    return run


def current_ids() -> tuple[str, str] | None:
    """(trace_id, span_id) as hex strings for the current span, or None."""
    if not _enabled:
        return None
    from opentelemetry import trace
    sc = trace.get_current_span().get_span_context()
    if not sc.is_valid:
        return None
    return format(sc.trace_id, "032x"), format(sc.span_id, "016x")
