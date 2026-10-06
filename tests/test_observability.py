"""Logging, request metrics and component up-metrics."""
import json
import logging
import re

import pytest

from test_rv_trials import fake, prov, start, submit  # noqa: F401  (fixtures)
from app import ai_feedback, config, observability
from app.observability import prober


def metric(client, name, **labels):
    """Value of one sample from /metrics, or None when the series is absent."""
    text = client.get("/metrics").text
    want = {k: str(v) for k, v in labels.items()}
    for line in text.splitlines():
        m = re.match(rf"^{re.escape(name)}(\{{(.*)\}})? ([0-9.eE+-]+|NaN)$", line)
        if not m:
            continue
        have = dict(re.findall(r'(\w+)="([^"]*)"', m.group(2) or ""))
        if all(have.get(k) == v for k, v in want.items()):
            return float(m.group(3))
    return None


def test_required_components_are_up_from_the_first_scrape(client):
    assert metric(client, "rv_component_up", component="database") == 1
    assert metric(client, "rv_component_up", component="data_volume") == 1
    assert metric(client, "rv_component_up", component="workers") == 1
    assert metric(client, "rv_ready") == 1
    assert metric(client, "rv_data_volume_free_bytes") > 0
    assert metric(client, "rv_info", version=observability.VERSION) == 1
    assert metric(client, "rv_component_last_success_timestamp_seconds", component="database") > 1e9


def test_status_endpoint(client):
    j = client.get("/api/status").json()
    assert j["ready"] is True and j["components"]["database"]["up"] is True and j["version"] == observability.VERSION


def test_broken_component_goes_down_and_recovers(client, monkeypatch, caplog):
    def boom():
        raise OSError("disk on fire")
    check = next(c for c in prober.checks if c.name == "database")
    good = check.fn
    check.fn = boom
    try:
        with caplog.at_level(logging.WARNING, logger="rv.obs"):
            prober.run_once(only=("database",))
        assert metric(client, "rv_component_up", component="database") == 0
        assert metric(client, "rv_ready") == 0
        assert "disk on fire" in client.get("/api/status").json()["components"]["database"]["detail"]
        assert any("DOWN" in r.getMessage() for r in caplog.records)         # transition is logged once
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="rv.obs"):
            prober.run_once(only=("database",))
        assert not caplog.records                                              # no repeat spam while still down
    finally:
        check.fn = good
    prober.run_once(only=("database",))
    assert metric(client, "rv_component_up", component="database") == 1 and metric(client, "rv_ready") == 1


def test_ai_components(client, monkeypatch):
    monkeypatch.setattr(config, "AI_BASE_URL", "")
    prober.run_once(only=("ai",))
    assert metric(client, "rv_component_up", component="ai_backend") is None      # not configured: no series
    assert metric(client, "rv_ai_enabled") == 0
    monkeypatch.setattr(config, "AI_BASE_URL", "http://x"); monkeypatch.setattr(config, "AI_MODEL", "m1")
    monkeypatch.setattr(ai_feedback, "health", lambda: {"ok": True, "model_available": True, "error": None})
    prober.run_once(only=("ai",))
    assert metric(client, "rv_component_up", component="ai_backend") == 1
    assert metric(client, "rv_component_up", component="ai_model") == 1
    assert metric(client, "rv_ai_enabled") == 1
    monkeypatch.setattr(ai_feedback, "health", lambda: {"ok": True, "model_available": False, "error": None})
    prober.run_once(only=("ai",))
    assert metric(client, "rv_component_up", component="ai_backend") == 1
    assert metric(client, "rv_component_up", component="ai_model") == 0
    monkeypatch.setattr(ai_feedback, "health", lambda: {"ok": False, "model_available": None, "error": "connection refused"})
    prober.run_once(only=("ai",))
    assert metric(client, "rv_component_up", component="ai_backend") == 0
    assert "refused" in client.get("/api/status").json()["components"]["ai_backend"]["detail"]
    monkeypatch.setattr(config, "AI_BASE_URL", "")
    prober.run_once(only=("ai",))
    assert metric(client, "rv_component_up", component="ai_backend") is None


def test_image_provider_component(client, prov, monkeypatch):
    prober.run_once(only=("image_provider",))
    assert metric(client, "rv_component_up", component="image_provider") == 1
    from test_rv_trials import Fake
    from app import targets
    targets._cache.clear()
    Fake.mode = "apifail"
    try:
        prober.run_once(only=("image_provider",))
    finally:
        Fake.mode = "ok"
    assert metric(client, "rv_component_up", component="image_provider") == 0


def test_http_metrics_use_route_templates_and_request_ids(client):
    r = client.get("/guide")
    assert r.status_code == 200 and re.match(r"^[0-9a-f]{16}$", r.headers["x-request-id"])
    r2 = client.get("/guide", headers={"X-Request-ID": "trace-12345678"})
    assert r2.headers["x-request-id"] == "trace-12345678"
    r3 = client.get("/guide", headers={"X-Request-ID": "bad id\nwith junk"})
    assert r3.headers["x-request-id"] != "bad id\nwith junk"
    assert metric(client, "rv_http_requests_total", method="GET", route="/guide", status="200") >= 3
    client.get("/rv/trials/doesnotexist1234")
    assert metric(client, "rv_http_requests_total", method="GET", route="/rv/trials/{trial_id}", status="404") >= 1
    client.get("/definitely/not/a/route")
    assert metric(client, "rv_http_requests_total", method="GET", route="unmatched", status="404") >= 1
    assert "doesnotexist1234" not in client.get("/metrics").text                  # concrete ids never become labels
    assert metric(client, "rv_http_request_duration_seconds_count", method="GET", route="/guide") >= 3


def test_access_log_and_probe_noise(client, caplog):
    with caplog.at_level(logging.DEBUG, logger="rv.http"):
        client.get("/healthz"); client.get("/guide?secret=hunter2")
    msgs = [(r.levelno, r.fields) for r in caplog.records if r.name == "rv.http" and hasattr(r, "fields")]
    assert any(l == logging.DEBUG and f["route"] == "/healthz" for l, f in msgs)      # probes stay quiet
    guide = next(f for l, f in msgs if f["route"] == "/guide")
    assert guide["status"] == 200 and guide["method"] == "GET" and "duration_ms" in guide
    assert "hunter2" not in json.dumps([f for _, f in msgs])                          # query strings are never logged


def test_json_formatter():
    rec = logging.LogRecord("rv.x", logging.WARNING, "f.py", 1, "hello %s", ("world",), None)
    rec.fields = {"trial_id": "abc", "msg": "must not override"}
    tok = observability.request_id_var.set("rid123456789")
    try:
        d = json.loads(observability.JsonFormatter().format(rec))
    finally:
        observability.request_id_var.reset(tok)
    assert d["msg"] == "hello world" and d["level"] == "WARNING" and d["trial_id"] == "abc" and d["request_id"] == "rid123456789"
    assert d["ts"].endswith("+00:00")
    t = observability.TextFormatter().format(rec)
    assert "WARNING" in t and "trial_id=abc" in t


def test_client_events(client):
    before = metric(client, "rv_client_events_total", event="bt_connect_failed") or 0
    r = client.post("/api/client-event", json={"event": "bt_connect_failed", "detail": "GATT Server is disconnected"})
    assert r.status_code == 204
    assert metric(client, "rv_client_events_total", event="bt_connect_failed") == before + 1
    for ev in ("bt_reconnected", "bt_reconnect_failed", "bt_disconnected"):
        assert client.post("/api/client-event", json={"event": ev, "detail": "recording, up 12s, battery 80%, samples 100"}).status_code == 204
    assert client.post("/api/client-event", json={"event": "drop_tables"}).status_code == 400        # whitelist
    assert client.post("/api/client-event", content=b"not json").status_code == 400
    assert client.post("/api/client-event", content=b"x" * 5000).status_code == 413
    assert "drop_tables" not in client.get("/metrics").text


def test_logs_never_reveal_the_target(client, prov, caplog):
    import httpx  # noqa: F401
    with caplog.at_level(logging.INFO):
        loc = start(client)
        tid = loc.rsplit("/", 1)[1]
        from test_rv_trials import trial_row
        tj = json.loads(trial_row(tid).target_json)
        secrets_ = [str(tj[k]) for k in ("title", "category") if tj.get(k)]
        assert secrets_
        text = "\n".join(r.getMessage() + json.dumps(getattr(r, "fields", {}), default=str) for r in caplog.records)
        for sec in secrets_:
            assert sec not in text
        assert "trial started" in text and tid in text
        assert submit(client, tid).status_code == 200
    assert any("trial submitted" in r.getMessage() for r in caplog.records)


def test_auth_failures_are_logged_without_credentials(client, monkeypatch, caplog):
    monkeypatch.setattr(config, "AUTH_USER", "u"); monkeypatch.setattr(config, "AUTH_PASSWORD", "p")
    with caplog.at_level(logging.WARNING, logger="rv.auth"):
        r = client.get("/guide", auth=("u", "wrong-pass-123"))
    assert r.status_code == 401
    assert any("authentication failed" in x.getMessage() for x in caplog.records)
    assert "wrong-pass-123" not in "".join(x.getMessage() + str(getattr(x, "fields", "")) for x in caplog.records)
    assert metric(client, "rv_http_requests_total", method="GET", route="/guide", status="401") >= 1
