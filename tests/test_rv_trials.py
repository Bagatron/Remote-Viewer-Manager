"""Tests for RV target trials against a fake Wikimedia/Pexels server.

Run:  pip install pytest && pytest -q tests/test_rv_trials.py
"""
import io
import json
import os
import re
import sys
import tempfile
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("RV_DATA_DIR", tempfile.mkdtemp(prefix="rvdata-"))
sys.path.insert(0, str(ROOT))

from app import config, targets  # noqa: E402
from app import main as rvmain  # noqa: E402
from app import rv_trials  # noqa: E402
from app.db import RVTrial, SessionLocal  # noqa: E402


def make_png(w=200, h=150) -> bytes:
    im = Image.new("RGB", (w, h), "white")
    ImageDraw.Draw(im).line((10, 10, w - 10, h - 10), fill="blue", width=5)
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def make_jpeg(seed: int) -> bytes:
    im = Image.new("RGB", (1280, 853), ((seed * 53) % 256, (seed * 97) % 256, (seed * 29) % 256))
    ImageDraw.Draw(im).ellipse((200, 150, 900, 700), fill=((seed * 7) % 256, 200, 100))
    b = io.BytesIO()
    im.save(b, "JPEG")
    return b.getvalue()


class Fake(BaseHTTPRequestHandler):
    mode = "ok"
    seen: list = []

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        Fake.seen.append({"path": u.path, "ua": self.headers.get("User-Agent"), "auth": self.headers.get("Authorization")})
        base = f"http://127.0.0.1:{self.server.server_port}"
        if u.path.startswith("/img/"):
            if Fake.mode == "badimg":
                body = b"<html>not an image</html>"
                ctype = "text/html"
            else:
                body, ctype = make_jpeg(int(u.path.split("/")[-1].split(".")[0])), "image/jpeg"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if Fake.mode == "apifail" and u.path in ("/w/api.php", "/v1/search"):
            return self._json(500, {"error": "down"})
        if u.path == "/w/api.php":
            seed = zlib.crc32(q["gsrsearch"][0].encode()) % 100000 * 100
            pages = []
            for i in range(30):
                pid = seed + i
                ii = {"thumburl": f"{base}/img/{pid}.jpg", "url": f"{base}/img/{pid}.jpg",
                      "descriptionurl": f"https://commons.example/File:Fake_{pid}.jpg",
                      "width": 4000, "height": 2667, "mime": "image/jpeg",
                      "extmetadata": {"LicenseShortName": {"value": "CC BY-SA 4.0"},
                                      "Artist": {"value": '<a href="x">Jane Photographer</a>'},
                                      "ImageDescription": {"value": "<p>A scenic view</p>"}}}
                title = f"File:Fake photo {pid}.jpg"
                if i == 0:
                    title = f"File:Portrait of a man {pid}.jpg"
                elif i == 1:
                    ii["width"] = 500
                elif i == 2:
                    ii["extmetadata"]["Restrictions"] = {"value": "personality rights"}
                elif i == 3:
                    ii["mime"] = "image/gif"
                elif i == 4:
                    del ii["extmetadata"]["LicenseShortName"]
                elif i == 5:
                    ii["extmetadata"]["GPSLatitude"] = {"value": "35.5"}
                    ii["extmetadata"]["GPSLongitude"] = {"value": "139.7"}
                pages.append({"pageid": pid, "ns": 6, "title": title, "index": i + 1, "imageinfo": [ii]})
            return self._json(200, {"batchcomplete": True, "query": {"pages": pages}})
        if u.path == "/v1/search":
            if self.headers.get("Authorization") != "pexkey":
                return self._json(401, {"error": "bad key"})
            seed = zlib.crc32(q["query"][0].encode()) % 100000 * 100
            photos = [{"id": seed + i, "width": 4000, "height": 2667, "url": f"https://pexels.example/{seed + i}",
                       "photographer": "Pat Px", "alt": "A group of people" if i == 0 else f"Calm scene {seed + i}",
                       "src": {"large2x": f"{base}/img/{seed + i}.jpg"}} for i in range(20)]
            return self._json(200, {"photos": photos})
        self._json(404, {})


@pytest.fixture(scope="module")
def fake():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture()
def prov(fake, monkeypatch):
    Fake.mode, Fake.seen = "ok", []
    targets._cache.clear()
    monkeypatch.setattr(config, "WIKIMEDIA_API", f"{fake}/w/api.php")
    monkeypatch.setattr(config, "PEXELS_API", f"{fake}/v1/search")
    monkeypatch.setattr(config, "PEXELS_API_KEY", "")
    monkeypatch.setattr(config, "TARGET_PROVIDER", "wikimedia")
    monkeypatch.setattr(config, "TARGET_TIMEOUT_S", 10)
    return fake


def start(client, judging=False):
    r = client.post("/rv/start", data={"judging": "1"} if judging else {}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r.headers["location"]


def trial_row(tid):
    with SessionLocal() as db:
        return db.get(RVTrial, tid)


def submit(client, tid, pages=1, **kw):
    data = {"confidence": kw.get("confidence", "60"), "notes": kw.get("notes", "blue, curved, cold")}
    files = [("pages", (f"p{i}.png", make_png(), "image/png")) for i in range(pages)]
    return client.post(f"/rv/trials/{tid}/submit", data=data, files=files or None)


# ---------------------------------------------------------------- pure functions
def test_binomial_p_value():
    assert rv_trials.binom_sf(3, 10, 0.25) == pytest.approx(0.4744, abs=1e-3)
    assert rv_trials.binom_sf(0, 5, 0.25) == pytest.approx(1.0)
    assert rv_trials.binom_sf(5, 5, 0.25) == pytest.approx(0.25**5)


def test_blocklist_keeps_targets_clean():
    assert targets.blocked("Portrait of a woman") and targets.blocked("crowd at a concert")
    assert not targets.blocked("A man-made lake at sunrise") and not targets.blocked("Warsaw skyline")


def test_wikimedia_filtering_and_metadata(prov):
    cands = targets._search_wikimedia("lighthouse", "tower")
    # 30 results minus: blocked title, too small, restricted, gif, no licence = 25
    assert len(cands) == 25
    assert all(c.license == "CC BY-SA 4.0" and c.creator == "Jane Photographer" for c in cands)
    gps = [c for c in cands if c.lat is not None]
    assert len(gps) == 1 and (gps[0].lat, gps[0].lon) == (35.5, 139.7)
    assert Fake.seen[0]["ua"].startswith("RV-Analyzer/")  # Wikimedia requires a descriptive UA


# ---------------------------------------------------------------- blind protocol
def test_blind_flow_end_to_end(client, prov):
    loc = start(client)
    tid = loc.rsplit("/", 1)[1]
    page = client.get(loc)
    assert page.status_code == 200 and re.search(r"\d{4}-\d{4}", page.text)
    assert "Fake photo" not in page.text and "img/target" not in page.text and "Jane Photographer" not in page.text
    assert client.get(f"/rv/trials/{tid}/img/target").status_code == 403
    assert client.get(f"/rv/trials/{tid}/img/opt0").status_code == 404
    api = client.get("/api/rv/trials").json()["trials"]
    mine = next(t for t in api if t["id"] == tid)
    assert mine["status"] == "assigned" and "target" not in mine

    r = submit(client, tid, pages=2)
    assert r.status_code == 200 and r.json()["next"] == f"/rv/trials/{tid}"
    assert submit(client, tid).status_code == 409                     # cannot resubmit
    row = trial_row(tid)
    assert row.status == "revealed" and row.confidence == 60 and row.n_pages == 2 and row.duration_s is not None

    page = client.get(loc)
    assert "Fake photo" in page.text and "Jane Photographer" in page.text and "CC BY-SA 4.0" in page.text
    img = client.get(f"/rv/trials/{tid}/img/target")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    assert Image.open(io.BytesIO(img.content)).size[0] <= 1600
    assert client.get(f"/rv/trials/{tid}/sketch/1").headers["content-type"] == "image/png"
    assert client.get(f"/rv/trials/{tid}/sketch/3").status_code == 404

    # scoring completes it; scores are editable
    assert client.post(f"/rv/trials/{tid}/score", data={"accuracy": "35", "feedback_notes": "water yes"},
                       follow_redirects=False).status_code == 303
    assert trial_row(tid).status == "complete" and trial_row(tid).accuracy == 35
    client.post(f"/rv/trials/{tid}/score", data={"accuracy": "40"}, follow_redirects=False)
    assert trial_row(tid).accuracy == 40
    assert client.post(f"/rv/trials/{tid}/score", data={"accuracy": "101"}).status_code == 400

    j = client.get("/api/rv/trials").json()
    assert j["stats"]["n_complete"] >= 1 and j["stats"]["mean_accuracy"] is not None
    assert next(t for t in j["trials"] if t["id"] == tid)["target"]["category"] in targets.THEMES
    index = client.get("/rv")
    assert index.status_code == 200 and rows_contain(index.text, tid)


def rows_contain(html, tid):
    return f"/rv/trials/{tid}" in html


def test_submit_validation(client, prov):
    tid = start(client).rsplit("/", 1)[1]
    assert submit(client, tid, confidence="150").status_code == 400
    assert client.post(f"/rv/trials/{tid}/submit", data={"confidence": "50", "notes": " "}).status_code == 400  # nothing
    bad = client.post(f"/rv/trials/{tid}/submit", data={"confidence": "50"},
                      files=[("pages", ("x.png", b"GIF89a....", "image/png"))])
    assert bad.status_code == 400
    fake_png = client.post(f"/rv/trials/{tid}/submit", data={"confidence": "50"},
                           files=[("pages", ("x.png", targets_png_header_only(), "image/png"))])
    assert fake_png.status_code == 400                                  # magic ok but not a real PNG
    assert submit(client, tid, pages=7).status_code == 400
    assert trial_row(tid).status == "assigned"                          # nothing was half-saved
    assert submit(client, tid, pages=0, notes="just words").status_code == 200  # notes-only is allowed


def targets_png_header_only():
    return b"\x89PNG\r\n\x1a\n" + b"garbage"


# ---------------------------------------------------------------- judging mode
def test_judging_hit_and_miss(client, prov):
    outcomes = []
    for want_hit in (True, False):
        tid = start(client, judging=True).rsplit("/", 1)[1]
        row = trial_row(tid)
        opts = row.options
        assert len(opts) == 2 and sum(o["is_target"] for o in opts) == 1
        assert len({o["category"] for o in opts}) == 2                  # the decoy comes from another category
        assert len({o["source_id"] for o in opts}) == 2
        assert submit(client, tid).status_code == 200
        assert trial_row(tid).status == "judging"

        page = client.get(f"/rv/trials/{tid}").text
        assert page.count("/img/opt") == 2 and "1 in 2" in page and "Fake photo" not in page and "/img/target" not in page
        assert client.get(f"/rv/trials/{tid}/img/target").status_code == 403
        assert client.get(f"/rv/trials/{tid}/img/opt0").status_code == 200
        assert client.post(f"/rv/trials/{tid}/score", data={"accuracy": "50"}).status_code == 409
        assert client.post(f"/rv/trials/{tid}/judge", data={"choice": "opt9"}).status_code == 400

        pick = next(o for o in opts if o["is_target"] == want_hit)["key"]
        assert client.post(f"/rv/trials/{tid}/judge", data={"choice": pick}, follow_redirects=False).status_code == 303
        row = trial_row(tid)
        assert row.status == "revealed" and row.judged_correct is want_hit
        assert client.get(f"/rv/trials/{tid}/img/target").status_code == 200
        assert "outline" in client.get(f"/rv/trials/{tid}").text or "picked" in client.get(f"/rv/trials/{tid}").text
        assert client.post(f"/rv/trials/{tid}/judge", data={"choice": pick}).status_code == 409
        outcomes.append(want_hit)
    s = client.get("/api/rv/trials").json()["stats"]
    assert s["judged_n"] >= 2 and s["judged_hits"] >= 1 and s["judged_p"] is not None


def test_judging_option_count_is_configurable(client, prov, monkeypatch):
    monkeypatch.setattr(config, "JUDGING_OPTIONS", 4)
    tid = start(client, judging=True).rsplit("/", 1)[1]
    opts = trial_row(tid).options
    assert len(opts) == 4 and len({o["category"] for o in opts}) == 4
    assert submit(client, tid).status_code == 200
    page = client.get(f"/rv/trials/{tid}").text
    assert page.count("/img/opt") == 4 and "1 in 4" in page


def test_stats_use_each_trials_own_chance_level():
    class T:
        def __init__(self, n, hit):
            self.status, self.judged_correct, self.options = "complete", hit, [{}] * n
            self.accuracy, self.confidence = 50, 50
            self.target = {"category": "x"}
    s = rv_trials.compute_stats([T(4, True), T(4, False), T(2, True), T(2, False)])
    assert s["judged_n"] == 4 and s["judged_hits"] == 2
    assert s["judged_chance_label"] == "mixed 25%/50%" and s["judged_chance"] == pytest.approx(0.375)
    # exact: P(X >= 2) with p = .25,.25,.5,.5
    assert s["judged_p"] == pytest.approx(rv_trials.poisson_binom_sf(2, [.25, .25, .5, .5]), abs=1e-4)
    assert rv_trials.poisson_binom_sf(3, [0.25] * 10) == pytest.approx(rv_trials.binom_sf(3, 10, 0.25))
    assert rv_trials.poisson_binom_sf(0, [.5, .5]) == pytest.approx(1.0)
    assert rv_trials.compute_stats([T(2, True)])["judged_chance_label"] == "50%"


# ---------------------------------------------------------------- providers, failures, housekeeping
def test_unique_coordinates_and_no_repeated_targets(client, prov):
    for _ in range(5):
        start(client)
    with SessionLocal() as db:
        rows = list(db.query(RVTrial).all())
    coords = [r.coordinate for r in rows]
    assert len(coords) == len(set(coords)) and all(re.fullmatch(r"\d{4}-\d{4}", c) for c in coords)
    ids = [r.target["source_id"] for r in rows]
    assert len(ids) == len(set(ids))


def test_pexels_provider(client, prov, monkeypatch):
    monkeypatch.setattr(config, "TARGET_PROVIDER", "pexels")
    monkeypatch.setattr(config, "PEXELS_API_KEY", "pexkey")
    tid = start(client).rsplit("/", 1)[1]
    t = trial_row(tid).target
    assert t["provider"] == "pexels" and t["license"] == "Pexels License" and t["creator"] == "Pat Px"
    assert "people" not in t["title"]
    assert any(s["auth"] == "pexkey" for s in Fake.seen)
    monkeypatch.setattr(config, "PEXELS_API_KEY", "")                   # pexels selected but no key
    r = client.post("/rv/start", data={}, follow_redirects=False)
    assert r.status_code == 303 and "/rv?error=" in r.headers["location"]


@pytest.mark.parametrize("mode", ["apifail", "badimg"])
def test_provider_failure_is_reported_and_nothing_is_created(client, prov, mode):
    Fake.mode = mode
    with SessionLocal() as db:
        before = db.query(RVTrial).count()
    r = client.post("/rv/start", data={}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/rv?error=")
    page = client.get(r.headers["location"])
    assert page.status_code == 200 and "prepare a target" in page.text
    with SessionLocal() as db:
        assert db.query(RVTrial).count() == before
    assert not list(Path(config.TRIALS_DIR).glob("*")) or all((p / "target.jpg").exists() for p in Path(config.TRIALS_DIR).glob("*"))


def test_probe_endpoint(client, prov):
    ok = client.get("/api/rv/targets/probe").json()
    assert ok["ok"] and ok["image_bytes"] > 1000 and "title" not in ok
    Fake.mode = "apifail"
    targets._cache.clear()
    bad = client.get("/api/rv/targets/probe").json()
    assert not bad["ok"] and bad["error"]


def test_abandon_and_delete(client, prov):
    tid = start(client).rsplit("/", 1)[1]
    client.post(f"/rv/trials/{tid}/abandon", follow_redirects=False)
    assert trial_row(tid).status == "abandoned"
    assert client.get(f"/rv/trials/{tid}/img/target").status_code == 200   # shown after giving up
    assert submit(client, tid).status_code == 409
    d = trial_row(tid).dir
    assert d.exists()
    client.post(f"/rv/trials/{tid}/delete", follow_redirects=False)
    assert trial_row(tid) is None and not d.exists()
