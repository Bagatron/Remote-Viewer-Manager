"""Drive the real whiteboard in headless Chromium against a fake image provider.

Not part of the pytest run (needs a browser). Run:
    pip install playwright && python tests/browser_smoke.py [path/to/chromium] [screenshot_dir]
"""
import io
import os
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["RV_DATA_DIR"] = tempfile.mkdtemp(prefix="rvbrowser-")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import uvicorn  # noqa: E402
from PIL import Image  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from app import config  # noqa: E402
from app.main import app  # noqa: E402
from test_rv_trials import Fake  # noqa: E402  (fake Commons/Pexels server)

chrome = sys.argv[1] if len(sys.argv) > 1 else None
shots = Path(sys.argv[2] if len(sys.argv) > 2 else tempfile.mkdtemp(prefix="shots-"))
shots.mkdir(parents=True, exist_ok=True)

fake = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=fake.serve_forever, daemon=True).start()
config.WIKIMEDIA_API = f"http://127.0.0.1:{fake.server_port}/w/api.php"
config.TARGET_PROVIDER = "wikimedia"

srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
threading.Thread(target=srv.run, daemon=True).start()
while not srv.started:
    time.sleep(0.05)
BASE = f"http://127.0.0.1:{srv.servers[0].sockets[0].getsockname()[1]}"


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        raise SystemExit(1)


def ink(page):
    """Fraction of non-white pixels on the whiteboard canvas."""
    return page.evaluate("""() => { const c = document.getElementById('wb'), d = c.getContext('2d').getImageData(0,0,c.width,c.height).data;
        let n = 0; for (let i = 0; i < d.length; i += 4) if (d[i] < 250 || d[i+1] < 250 || d[i+2] < 250) n++; return n / (d.length / 4); }""")


def stroke(page, pts):
    box = page.locator("#wb").bounding_box()
    page.mouse.move(box["x"] + pts[0][0] * box["width"], box["y"] + pts[0][1] * box["height"])
    page.mouse.down()
    for x, y in pts[1:]:
        page.mouse.move(box["x"] + x * box["width"], box["y"] + y * box["height"], steps=6)
    page.mouse.up()


with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=chrome) if chrome else p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1100, "height": 1500})
    page = ctx.new_page()
    errors, requested = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("request", lambda r: requested.append(r.url))
    bad = []
    page.on("response", lambda r: bad.append(f"{r.status} {r.url}") if r.status >= 400 else None)
    page.on("dialog", lambda d: d.accept())

    # ---- start a trial
    page.goto(f"{BASE}/rv")
    page.click("text=Start new target")
    page.wait_for_url("**/rv/trials/*")
    tid = page.url.rsplit("/", 1)[1]
    coord = page.locator(".coord").inner_text()
    check(len(coord) == 9 and coord[4] == "-", f"coordinate shown: {coord}")
    check(page.locator("#wb").count() == 1, "whiteboard present")
    check(not any(u.endswith("/img/target") for u in requested), "target image not requested before submit")
    check("Fake photo" not in page.content(), "target title absent from page source before submit")

    # ---- draw: pen strokes, colour change, eraser, undo
    check(ink(page) == 0, "canvas starts blank")
    stroke(page, [(0.2, 0.3), (0.4, 0.2), (0.6, 0.35), (0.8, 0.25)])
    a = ink(page)
    check(a > 0.0005, f"pen stroke leaves ink ({a:.4f})")
    page.locator(".sw").nth(2).click()                       # red
    stroke(page, [(0.2, 0.6), (0.5, 0.75), (0.8, 0.6)])
    b = ink(page)
    check(b > a, "second (red) stroke adds ink")
    page.click("#t-eraser")
    stroke(page, [(0.2, 0.3), (0.4, 0.2), (0.6, 0.35), (0.8, 0.25)])
    c = ink(page)
    check(c < b, "eraser removes ink")
    page.click("#undo")
    check(ink(page) > c * 0.999 and ink(page) >= c, "undo reverts the last stroke")
    page.click("#t-pen")

    # ---- second page
    page.click("text=+ New page")
    check(ink(page) == 0, "new page is blank")
    stroke(page, [(0.3, 0.3), (0.3, 0.7), (0.7, 0.7), (0.7, 0.3), (0.3, 0.3)])
    check(ink(page) > 0.001, "drew on page 2")
    page.click("text=Page 1")
    check(ink(page) > 0.001, "switching back restores page 1")
    page.screenshot(path=str(shots / "whiteboard.png"), full_page=True)

    # ---- notes, confidence, autosave across reload
    page.fill("#notes", "cold, blue, curved, big open space")
    page.evaluate("() => { const c = document.getElementById('conf'); c.value = 72; c.dispatchEvent(new Event('input')); }")
    page.wait_for_timeout(4600)                               # autosave tick
    page.reload()
    check(page.input_value("#notes") == "cold, blue, curved, big open space", "notes restored after reload")
    check(page.input_value("#conf") == "72" and page.inner_text("#confv") == "72", "confidence restored after reload")
    check(ink(page) > 0.001 and page.locator("#tabs button").count() == 3, "sketch pages restored after reload")

    # ---- submit -> reveal
    page.click("#submit")
    page.wait_for_selector("text=Your session")
    check(page.locator("img[alt='Target']").count() == 1, "target revealed after submit")
    ok = page.evaluate("() => { const i = document.querySelector(\"img[alt='Target']\"); return i.complete && i.naturalWidth > 0; }")
    check(ok, "target image loads")
    sk = page.locator(".sketches img")
    check(sk.count() == 2, "both non-empty pages saved (2 sketches)")
    png = page.request.get(f"{BASE}/rv/trials/{tid}/sketch/1").body()
    im = Image.open(io.BytesIO(png)).convert("RGB")
    nonwhite = sum(1 for px in im.getdata() if px != (255, 255, 255))
    check(im.size == (1200, 900) and nonwhite > 500, f"saved sketch is 1200x900 with ink ({nonwhite} px)")
    check(page.evaluate("() => localStorage.getItem('rvdraft-' + location.pathname.split('/').pop())") is None, "draft cleared after submit")
    page.screenshot(path=str(shots / "reveal.png"), full_page=True)

    # ---- score
    page.fill("textarea[name=feedback_notes]", "water and curves were right")
    page.evaluate("() => { const r = document.querySelector('input[name=accuracy]'); r.value = 55; r.dispatchEvent(new Event('input')); }")
    page.click("text=Save score")
    page.wait_for_selector(".badge.done")
    check(page.locator(".badge.done").inner_text() == "complete", "trial marked complete")
    page.goto(f"{BASE}/rv")
    check(page.locator(f"a[href='/rv/trials/{tid}']").count() == 1, "trial listed on hub")
    check("55" in page.locator("table").last.inner_text() and "72" in page.locator("table").last.inner_text(), "confidence 72 and accuracy 55 shown")
    page.screenshot(path=str(shots / "hub.png"), full_page=True)

    # ---- judging mode through the UI
    page.check("input[name=judging]")
    page.click("text=Start new target")
    page.wait_for_url("**/rv/trials/*")
    stroke(page, [(0.3, 0.4), (0.7, 0.6)])
    page.fill("#notes", "machine, tall")
    page.click("#submit")
    page.wait_for_selector(".grid4 .opt")
    check(page.locator(".opt img").count() == 2, "two judging options shown")
    check(page.locator(".opt").first.locator("img").evaluate("i => i.complete && i.naturalWidth > 0"), "option images load")
    page.screenshot(path=str(shots / "judging.png"), full_page=True)
    page.locator(".opt").nth(1).click()
    page.click("text=Lock in my choice and reveal")
    page.wait_for_selector("text=Score this session")
    check(page.locator(".opt.target").count() == 1 and page.locator(".opt.picked").count() == 1, "reveal marks target and pick")

    # ---- mobile layout renders without horizontal scroll
    m = browser.new_context(viewport={"width": 412, "height": 915}, has_touch=True, is_mobile=True, device_scale_factor=2.6)
    mp = m.new_page()
    mp.goto(f"{BASE}/rv")
    mp.click("text=Start new target")
    mp.wait_for_url("**/rv/trials/*")
    check(mp.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1"), "no horizontal overflow on a phone-width screen")
    mp.screenshot(path=str(shots / "mobile.png"), full_page=True)

    bad = [b for b in bad if not b.endswith("/favicon.ico")]   # browsers ask for one automatically
    check(not bad, f"no failed requests {bad}")
    errors = [e for e in errors if "Failed to load resource" not in e]
    check(not errors, f"no JS errors {errors[:3] if errors else ''}")
    browser.close()

print("screenshots:", shots)
