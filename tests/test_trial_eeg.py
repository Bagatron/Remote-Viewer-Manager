"""Brainwave capture attached to RV trials: blind protocol, processing, upload, series, AI."""
import io
import time

import numpy as np
import pytest

from test_rv_trials import Fake, fake, prov, start, submit, trial_row, make_png  # noqa: F401  (fixtures)
from app import ai_feedback, config, trial_eeg


def capture_csv(n=60, markers=True) -> bytes:
    """What static/muse.js writes: 5 bands x 4 channels (log10 uV^2), HSI, HeadBandOn, Elements."""
    rng = np.random.default_rng(1)
    bands = ["Delta", "Theta", "Alpha", "Beta", "Gamma"]
    head = ["TimeStamp"] + [f"{b}_{c}" for b in bands for c in ["TP9", "AF7", "AF8", "TP10"]] + \
           [f"HSI_{c}" for c in ["TP9", "AF7", "AF8", "TP10"]] + ["HeadBandOn", "Elements"]
    lines = [",".join(head)]
    for i in range(n):
        vals = [f"{1.2 + 0.3 * np.sin(i / 8) + rng.normal(0, .05):.4f}" for _ in range(20)]
        lines.append(f"2026-10-05 10:00:{i % 60:02d}.000,{','.join(vals)},1,1,1,1,1,")
        if markers and i in (10, 30):
            lines.append(f"2026-10-05 10:00:{i % 60:02d}.500" + "," * (len(head) - 2) + f",/Marker/{'begin viewing' if i == 10 else 'sketching'}")
    return ("\n".join(lines) + "\n").encode()


def wait_done(client, tid, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/api/rv/trials/{tid}/eeg").json()
        if j.get("present") and j["status"]["status"] != "pending":
            return j
        time.sleep(0.2)
    raise AssertionError("EEG processing did not finish")


def submit_with_eeg(client, tid, data):
    return client.post(f"/rv/trials/{tid}/submit", data={"confidence": "50", "notes": "wavy"},
                       files=[("pages", ("p.png", make_png(), "image/png")), ("eeg", ("capture.csv", data, "text/csv"))])


def test_blind_until_reveal_then_graph(client, prov):
    loc = start(client)
    tid = loc.rsplit("/", 1)[1]
    assert client.get(f"/api/rv/trials/{tid}/eeg").status_code == 403           # nothing leaks pre-reveal
    assert client.get(f"/rv/trials/{tid}/eeg/eeg_source.csv").status_code == 403
    assert client.post(f"/rv/trials/{tid}/eeg/upload", files={"file": ("a.csv", capture_csv(), "text/csv")}).status_code == 403
    assert submit_with_eeg(client, tid, capture_csv()).status_code == 200
    j = wait_done(client, tid)
    assert j["status"]["status"] == "done" and j["status"]["origin"] == "bluetooth"
    s = j["series"]
    assert s and len(s["t"]) >= 30 and set(s["bands"]) >= {"Delta", "Theta", "Alpha", "Beta", "Gamma"}
    labels = [e["label"] if isinstance(e, dict) else e for e in s["events"]]
    assert any("begin viewing" in str(l) for l in labels) and any("sketching" in str(l) for l in labels)
    assert client.get(f"/rv/trials/{tid}/eeg/eeg_source.csv").status_code == 200
    assert client.get(f"/rv/trials/{tid}/eeg/../../etc/passwd").status_code in (404, 422)
    assert client.get(f"/rv/trials/{tid}/eeg/secret.txt").status_code == 404
    row = next(t for t in client.get("/api/rv/trials").json()["trials"] if t["id"] == tid)
    assert row["eeg"] == "done"
    page = client.get(loc).text
    assert "Brainwaves" in page and "The five brainwave bands" in page


def test_ai_notes_never_contain_target(client, prov):
    loc = start(client)
    tid = loc.rsplit("/", 1)[1]
    assert submit_with_eeg(client, tid, capture_csv()).status_code == 200
    wait_done(client, tid)
    t = trial_row(tid)
    notes = trial_eeg.notes_for(t)
    assert "wavy" in notes
    import json
    tj = json.loads(t.target_json)
    secrets = [str(tj.get(k)) for k in ("title", "category", "query", "description") if tj.get(k)]
    assert secrets, "target data should exist"
    for secret in secrets:
        assert secret not in notes


def test_bad_recording_rejected_or_reported(client, prov):
    tid = start(client).rsplit("/", 1)[1]
    assert submit_with_eeg(client, tid, b"hello,world\n1,2\n").status_code == 400     # not a TimeStamp CSV
    assert trial_row(tid).status == "assigned"                                          # trial untouched
    assert submit_with_eeg(client, tid, b"TimeStamp,Elements\n2026-10-05 10:00:00.000,\n").status_code == 200
    j = wait_done(client, tid)
    assert j["status"]["status"] == "failed" and j["status"]["error"]
    assert j["series"] is None


def test_attach_later_and_replace(client, prov):
    tid = start(client).rsplit("/", 1)[1]
    assert submit(client, tid).status_code == 200                                      # no capture
    assert client.get(f"/api/rv/trials/{tid}/eeg").json()["present"] is False
    r = client.post(f"/rv/trials/{tid}/eeg/upload", files={"file": ("x.txt", b"abc", "text/plain")})
    assert r.status_code == 400
    r = client.post(f"/rv/trials/{tid}/eeg/upload", files={"file": ("m.csv", capture_csv(40), "text/csv")}, follow_redirects=False)
    assert r.status_code == 303
    j = wait_done(client, tid)
    assert j["status"]["status"] == "done" and j["status"]["origin"] == "upload"


def test_ai_regenerate(client, prov, monkeypatch):
    tid = start(client).rsplit("/", 1)[1]
    assert submit_with_eeg(client, tid, capture_csv()).status_code == 200
    wait_done(client, tid)
    monkeypatch.setattr(ai_feedback, "enabled", lambda: False)
    assert client.post(f"/rv/trials/{tid}/eeg/ai", data={"focus": ""}).status_code == 503


def test_trend_graph_includes_trial_recordings(client, prov):
    from app import main as rvmain
    before = [e for e in rvmain._trend_entries() if e["kind"] == "trial"]
    loc = start(client)
    tid = loc.rsplit("/", 1)[1]
    assert submit_with_eeg(client, tid, capture_csv()).status_code == 200
    wait_done(client, tid)
    t = trial_row(tid)
    entries = rvmain._trend_entries()
    trials = [e for e in entries if e["kind"] == "trial"]
    assert len(trials) == len(before) + 1
    assert any(e["name"] == f"trial {t.coordinate}" for e in trials)
    assert [e["when"].timestamp() for e in entries] == sorted(e["when"].timestamp() for e in entries)
    # the main page now has a trend panel that mentions both kinds, and the image renders
    page = client.get("/").text
    assert "Trend across sessions and trials" in page and "captured during RV trials" in page
    r = client.get("/trend.png")
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    # nothing about the hidden target is in the page's trend text before or after (names are coordinates only)
    assert t.target["title"] not in page


def test_trend_ignores_failed_and_unfinished_trial_recordings(client, prov):
    from app import main as rvmain
    n = len([e for e in rvmain._trend_entries() if e["kind"] == "trial"])
    loc = start(client)
    tid = loc.rsplit("/", 1)[1]                      # assigned, never submitted: no recording
    assert len([e for e in rvmain._trend_entries() if e["kind"] == "trial"]) == n
    loc2 = start(client)
    tid2 = loc2.rsplit("/", 1)[1]
    assert submit_with_eeg(client, tid2, b"not,a,recording\n1,2,3\n").status_code in (200, 400, 422)
    time.sleep(1.5)
    assert len([e for e in rvmain._trend_entries() if e["kind"] == "trial"]) == n


def test_plot_trend_handles_mixed_and_session_only():
    from app import analysis
    mixed = [{"name": "a", "rv_raw_mean": 0.1, "kind": "session"}, {"name": "trial 1111-2222", "rv_raw_mean": 0.3, "kind": "trial"}]
    assert analysis.plot_trend(mixed)[:4] == b"\x89PNG"
    assert analysis.plot_trend([{"name": "a", "rv_raw_mean": 0.1}])[:4] == b"\x89PNG"     # old callers (no kind)
    assert analysis.plot_trend([]) is None
    many = [{"name": f"s{i}", "rv_raw_mean": i / 10, "kind": "session"} for i in range(200)]
    assert analysis.plot_trend(many)[:4] == b"\x89PNG"
