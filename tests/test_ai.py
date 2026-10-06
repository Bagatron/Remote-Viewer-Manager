"""End-to-end tests for the AI coach against a fake Open WebUI / Ollama server.

Run:  pip install pytest && pytest -q tests/test_ai.py
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
os.environ["RV_DATA_DIR"] = tempfile.mkdtemp(prefix="rvdata-")
sys.path.insert(0, str(ROOT))

from app import ai_feedback, config  # noqa: E402
from app import main as rvmain  # noqa: E402

GOOD = ("## Snapshot\nTheta rose after **Marker 1** (00:60).\n\n## What stood out\n- 01:00 theta up\n"
        "- <script>alert(1)</script>\n\n## Caveats\nSmall sample.")


class Fake(BaseHTTPRequestHandler):
    mode = "ok"
    requests: list = []

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/models":
            return self._send(200, {"data": [{"id": "fake-model"}, {"id": "other"}]})
        if self.path == "/api/tags":
            return self._send(200, {"models": [{"name": "fake-model"}]})
        self._send(404, {})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n))
        Fake.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        m = Fake.mode
        if m == "slow":
            time.sleep(3)
        if m == "http500":
            return self._send(500, {"error": "boom"})
        if m == "401":
            return self._send(401, {"detail": "bad key"})
        text = {"ok": GOOD, "empty": "", "think": "<think>hmm</think>" + GOOD}.get(m, GOOD)
        if self.path == "/api/chat":  # ollama shape
            return self._send(200, {"message": {"role": "assistant", "content": text}})
        self._send(200, {"choices": [{"message": {"role": "assistant", "content": text}}]})


@pytest.fixture(scope="module")
def fake():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture()
def cfg(fake, monkeypatch):
    Fake.mode, Fake.requests = "ok", []
    monkeypatch.setattr(config, "AI_BASE_URL", fake)
    monkeypatch.setattr(config, "AI_MODEL", "fake-model")
    monkeypatch.setattr(config, "AI_BACKEND", "openwebui")
    monkeypatch.setattr(config, "AI_API_KEY", "sk-test")
    monkeypatch.setattr(config, "AI_TIMEOUT_S", 30)
    monkeypatch.setattr(config, "AI_AUTO", True)


@pytest.fixture(scope="module")
def samples():
    d = tempfile.mkdtemp(prefix="rvsample-")
    subprocess.run([sys.executable, str(ROOT / "tests" / "make_sample.py"), d], check=True, capture_output=True)
    return Path(d)


def wait(pred, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.2)
    raise AssertionError("timed out waiting")


def upload(client, path):
    with open(path, "rb") as f:
        r = client.post("/upload", files={"files": (path.name, f)}, follow_redirects=False)
    assert r.status_code == 303, r.text
    sid = r.headers["location"].rsplit("/", 1)[1]
    wait(lambda: next(s for s in client.get("/api/sessions").json() if s["id"] == sid)["status"] == "done")
    return sid


def ai_state(client, sid):
    return client.get(f"/api/sessions/{sid}/ai").json()


def test_health(client, cfg):
    h = client.get("/api/ai/health").json()
    assert h["ok"] and h["model_available"] and "fake-model" in h["models"]


def test_health_unconfigured(client, cfg, monkeypatch):
    monkeypatch.setattr(config, "AI_MODEL", "")
    h = client.get("/api/ai/health").json()
    assert not h["enabled"] and not h["ok"]


def test_auto_feedback_and_history(client, cfg, samples):
    s1 = upload(client, samples / "session_mindmonitor.csv")
    st = wait(lambda: (ai_state(client, s1)["status"] or {}).get("status") in ("done", "failed") and ai_state(client, s1))
    assert st["status"]["status"] == "done", st
    assert "Snapshot" in st["feedback_markdown"]

    req = Fake.requests[0]
    assert req["path"] == "/api/chat/completions" and req["auth"] == "Bearer sk-test"
    sys_msg, user_msg = req["body"]["messages"]
    assert sys_msg["role"] == "system" and "remote viewing" in sys_msg["content"]
    assert "theta_minus_beta" in user_msg["content"] and "/Marker/1" in user_msg["content"]
    assert "none" in user_msg["content"].split("## Earlier sessions")[1][:80]  # first session: no history

    # second session sees the first one in its history, plus notes
    client.post(f"/sessions/{s1}/notes", data={"notes": "Target was a lighthouse"})
    s2 = upload(client, samples / "session_excel.xlsx")
    wait(lambda: (ai_state(client, s2)["status"] or {}).get("status") == "done")
    user2 = Fake.requests[-1]["body"]["messages"][1]["content"]
    assert "session_mindmonitor" in user2 and "lighthouse" in user2
    assert (Path(config.SESSIONS_DIR) / s2 / "ai_prompt.txt").exists()


def test_session_page_escapes_model_html(client, cfg, samples):
    sid = upload(client, samples / "session_mindmonitor.csv")
    wait(lambda: (ai_state(client, sid)["status"] or {}).get("status") == "done")
    html = client.get(f"/sessions/{sid}").text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
    assert "<strong>Marker 1</strong>" in html and "AI coach" in html


@pytest.mark.parametrize("mode,needle", [("http500", "HTTP 500"), ("401", "RV_AI_API_KEY"),
                                         ("empty", "empty response")])
def test_failures_are_reported_not_fatal(client, cfg, samples, mode, needle):
    Fake.mode = mode
    sid = upload(client, samples / "session_mindmonitor.csv")
    st = wait(lambda: (ai_state(client, sid)["status"] or {}).get("status") == "failed" and ai_state(client, sid))
    assert needle in st["status"]["error"]
    page = client.get(f"/sessions/{sid}")
    assert page.status_code == 200 and "AI feedback failed" in page.text
    # analysis itself is unaffected, and a retry works once the model recovers
    Fake.mode = "ok"
    client.post(f"/sessions/{sid}/ai-feedback", data={"focus": "why so scattered?"}, follow_redirects=False)
    wait(lambda: (ai_state(client, sid)["status"] or {}).get("status") == "done")
    assert "why so scattered?" in Fake.requests[-1]["body"]["messages"][1]["content"]


def test_timeout_and_unreachable(client, cfg, samples, monkeypatch):
    Fake.mode = "slow"
    monkeypatch.setattr(config, "AI_TIMEOUT_S", 1)
    sid = upload(client, samples / "session_mindmonitor.csv")
    st = wait(lambda: (ai_state(client, sid)["status"] or {}).get("status") == "failed" and ai_state(client, sid))
    assert "Timed out" in st["status"]["error"]
    Fake.mode = "ok"
    monkeypatch.setattr(config, "AI_BASE_URL", "http://127.0.0.1:9")  # nothing listening
    client.post(f"/sessions/{sid}/ai-feedback", data={}, follow_redirects=False)
    st = wait(lambda: (ai_state(client, sid)["status"] or {}).get("status") == "failed" and ai_state(client, sid))
    assert "Could not reach" in st["status"]["error"]


def test_think_blocks_stripped_and_ollama_backend(client, cfg, samples, monkeypatch):
    Fake.mode = "think"
    monkeypatch.setattr(config, "AI_BACKEND", "ollama")
    sid = upload(client, samples / "session_mindmonitor.csv")
    st = wait(lambda: (ai_state(client, sid)["status"] or {}).get("status") == "done" and ai_state(client, sid))
    assert "<think>" not in st["feedback_markdown"]
    req = Fake.requests[-1]
    assert req["path"] == "/api/chat" and req["body"]["options"]["num_ctx"] == config.AI_NUM_CTX


def test_disabled_keeps_generic_tips_and_rejects_manual_run(client, cfg, samples, monkeypatch):
    monkeypatch.setattr(config, "AI_MODEL", "")
    sid = upload(client, samples / "session_mindmonitor.csv")
    assert ai_state(client, sid)["status"] is None
    page = client.get(f"/sessions/{sid}").text
    assert "isn't configured" in page and "Pause here to sketch" in page
    r = client.post(f"/sessions/{sid}/ai-feedback", data={}, follow_redirects=False)
    assert r.status_code == 503


def test_summary_no_longer_contains_canned_tips(client, cfg, samples):
    sid = upload(client, samples / "session_mindmonitor.csv")
    txt = (Path(config.SESSIONS_DIR) / sid / "coaching_summary.txt").read_text()
    assert "Pause here to sketch" not in txt and "RV_index_z=" in txt
