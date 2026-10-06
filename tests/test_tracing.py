"""Tracing runs in a subprocess so its global instrumentation cannot leak into other tests."""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCRIPT = textwrap.dedent('''
    import json, sys
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from fastapi.testclient import TestClient
    from app import main, tracing
    exp = InMemorySpanExporter()
    assert tracing.setup(main.app, engine=main.engine, exporter=exp)
    with TestClient(main.app) as c:
        r = c.get("/api/status?secret=abc")
        c.get("/healthz"); c.get("/metrics")
        trace_hdr = r.headers.get("X-Trace-ID")
        with tracing.span("probe.test"):
            ids = tracing.current_ids()
    spans = exp.get_finished_spans()
    out = {
        "trace_hdr": trace_hdr,
        "ids": ids,
        "names": [s.name for s in spans],
        "attrs": [dict(s.attributes or {}) for s in spans],
        "traces": sorted({format(s.context.trace_id, "032x") for s in spans}),
    }
    print("RESULT" + json.dumps(out, default=str))
''')


def test_tracing_spans_headers_and_scrubbing():
    with tempfile.TemporaryDirectory() as d:
        env = {**os.environ, "RV_DATA_DIR": d, "RV_PROBES": "false", "PYTHONPATH": str(ROOT)}
        env.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
        p = subprocess.run([sys.executable, "-c", SCRIPT], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr[-2000:]
    out = json.loads(next(l for l in p.stdout.splitlines() if l.startswith("RESULT"))[6:])
    assert out["trace_hdr"] and len(out["trace_hdr"]) == 32
    assert out["trace_hdr"] in out["traces"]
    assert any("api/status" in n for n in out["names"])
    blob = json.dumps(out["attrs"])
    assert "secret=abc" not in blob and "?" not in blob
    # health and metrics endpoints are not traced
    assert not any("healthz" in n or "metrics" in n for n in out["names"])


def test_tracing_off_by_default():
    from app import tracing
    assert not tracing.configured() or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    with tracing.span("x") as sp:
        assert sp is None
    assert tracing.current_ids() is None
    f = lambda: 1
    assert tracing.bind(f) is f
