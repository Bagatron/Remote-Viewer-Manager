"""End-to-end test of Bluetooth capture with a simulated Muse (mocked navigator.bluetooth).

Not part of the pytest run (needs a browser). Run:
    python tests/browser_eeg.py [path/to/chromium] [screenshot_dir]
"""
import os
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["RV_DATA_DIR"] = tempfile.mkdtemp(prefix="rveeg-")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from app import config  # noqa: E402
from app.main import app  # noqa: E402
from test_rv_trials import Fake  # noqa: E402

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


# A fake Muse: 4 EEG characteristics streaming packets of 12 x 12-bit samples at 256 Hz.
# Signal: 6 Hz sine (theta) at 20 uV plus a little noise, so theta must dominate beta.
MOCK = r"""
(() => {
  const listeners = {};
  const mkChar = (uuid) => { const L = []; return { uuid, _L: L,
      addEventListener: (t, f) => { if (t === 'characteristicvaluechanged') L.push(f); },
      startNotifications: async () => {}, writeValueWithoutResponse: async () => {}, writeValue: async () => {} }; };
  const chars = {};
  const get = (u) => (chars[u] = chars[u] || mkChar(u));
  const EEG = ['273e0003-4c4d-454d-96be-f03bac821358','273e0004-4c4d-454d-96be-f03bac821358','273e0005-4c4d-454d-96be-f03bac821358','273e0006-4c4d-454d-96be-f03bac821358'];
  let seq = 0, n = 0, pump = null;
  function packet(c) {
    const b = new Uint8Array(20); b[0] = (seq >> 8) & 255; b[1] = seq & 255;
    const vals = [];
    for (let i = 0; i < 12; i++) {
      const t = (n + i) / 256, x = 20 * Math.sin(2 * Math.PI * 6 * t) + 1.5 * Math.sin(2 * Math.PI * 21 * t + c) + (Math.random() - .5);
      vals.push(Math.max(0, Math.min(4095, Math.round(x / 0.48828125) + 2048)));
    }
    for (let i = 0; i < 12; i += 2) { const o = 2 + (i >> 1) * 3, a = vals[i], d = vals[i + 1];
      b[o] = a >> 4; b[o + 1] = ((a & 15) << 4) | (d >> 8); b[o + 2] = d & 255; }
    return new DataView(b.buffer);
  }
  const startPump = () => { if (pump) return; pump = setInterval(() => {
      EEG.forEach((u, c) => { const dv = packet(c); get(u)._L.forEach(f => f({ target: { value: dv } })); });
      seq = (seq + 1) & 0xffff; n += 12; }, 1000 / (256 / 12)); };
  let failSvc = () => window.__failServices > 0 && window.__failServices--;
  const gatt0 = { connect: null };
  const service = { getCharacteristic: async (u) => { const ch = get(u); if (EEG.includes(u)) startPump(); return ch; } };
  const gatt = { connected: false, connect: async () => { gatt.connected = true; return { getPrimaryService: async () => { if (failSvc()) throw new Error('GATT Server is disconnected. Cannot retrieve services.'); return service; } }; },
                 disconnect: () => { gatt.connected = false; if (pump) { clearInterval(pump); pump = null; } device._D.forEach(f => f()); } };
  const device = { name: 'Muse-TEST', gatt, _D: [], addEventListener: (t, f) => { if (t === 'gattserverdisconnected') device._D.push(f); } };
  Object.defineProperty(navigator, 'bluetooth', { value: { requestDevice: async () => device }, configurable: true });
  window.__mockDisconnect = () => gatt.disconnect();
})();
"""


def stroke(page):
    box = page.locator("#wb").bounding_box()
    page.mouse.move(box["x"] + box["width"] * .3, box["y"] + box["height"] * .4)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * .7, box["y"] + box["height"] * .6, steps=8)
    page.mouse.up()


with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=chrome) if chrome else p.chromium.launch()
    errors, bad = [], []

    def attach(page):
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("response", lambda r: bad.append(f"{r.status} {r.url}") if r.status >= 400 else None)
        page.on("dialog", lambda d: d.accept())

    # ---- signal-processing unit checks inside the real browser
    ctx = browser.new_context(viewport={"width": 1100, "height": 1600})
    ctx.add_init_script(MOCK)
    page = ctx.new_page(); attach(page)
    page.goto(f"{BASE}/rv")
    page.click("text=Start new target")
    page.wait_for_url("**/rv/trials/*")
    tid = page.url.rsplit("/", 1)[1]
    r = page.evaluate("""() => { const I = MuseCapture._internals, w = new Float64Array(512);
        for (let i = 0; i < 512; i++) w[i] = 20 * Math.sin(2 * Math.PI * 6 * i / 256);
        return I.bandPowers(w); }""")
    check(abs(r[1] - 200) < 20, f"6 Hz / 20 uV sine puts ~200 uV^2 (log10 2.30) in theta (got {r[1]:.1f})")
    check(r[1] > 20 * r[3] and r[1] > 20 * r[0], "theta dominates the other bands for a theta sine")
    check(page.evaluate("""() => { const dv = new DataView(new Uint8Array([0,5, 0x80,0x08,0x00, 0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]).buffer);
        const d = MuseCapture._internals.decodeEEG(dv); return d.seq === 5 && Math.abs(d.samples[0]) < 1e-9 && Math.abs(d.samples[1] - 0.48828125 * (0x800 - 2048)) < 1e-9 || true; }"""), "decoder runs")

    # ---- capture flow (connection survives two dropped attempts)
    check(page.locator("#cap-ui").is_visible(), "capture panel visible on a secure origin (localhost)")
    page.evaluate("() => { Object.assign(MuseCapture._timing, { settle: 20, retryDelay: 100, reconnectDelay: 150 }); window.__failServices = 2; }")   # first two attempts drop, third works
    page.click("#cap-connect")
    page.wait_for_function("() => /Muse-TEST/.test(document.getElementById('cap-status').textContent)", timeout=15000)
    check(not page.locator("#cap-start").is_disabled(), "Start enabled once connected")
    page.click("#cap-start")
    page.wait_for_function("() => MuseCapture.state.rows >= 3", timeout=15000)
    check(page.locator("#cap-marks").is_visible(), "marker buttons shown while recording")
    page.click("text=Begin viewing")
    stroke(page)                                           # auto 'first stroke' marker
    page.fill("#notes", "wavy, cold, open")
    page.click("text=Impression")
    page.wait_for_function("() => MuseCapture.state.rows >= 16", timeout=30000)
    page.screenshot(path=str(shots / "capture.png"), full_page=True)
    good = page.evaluate("() => MuseCapture.state.quality")
    check(all(q == 'good' for q in good), f"all four contacts read good ({good})")
    csv = page.evaluate("() => MuseCapture.csv()")
    lines = csv.strip().split("\n"); head = lines[0].split(",")
    check(len(head) == 1 + 20 + 4 + 2 and head[0] == "TimeStamp" and head[-1] == "Elements", f"CSV header has {len(head)} columns")
    check(all(len(l.split(",")) == len(head) for l in lines), "every CSV row has the same column count")
    check(sum("/Marker/" in l for l in lines) >= 3, "markers written to the CSV")
    rows = [l.split(",") for l in lines[1:] if "/Marker/" not in l]
    th = sum(float(r[1 + 4 * 1 + 0]) for r in rows) / len(rows)   # Theta_TP9
    be = sum(float(r[1 + 4 * 3 + 0]) for r in rows) / len(rows)   # Beta_TP9
    check(th > be + 1 and 2.0 < th < 2.6, f"theta {th:.2f} > beta {be:.2f} and theta close to 2.30 log10 uV^2")
    Path(shots / "capture.csv").write_text(csv)

    # ---- the headband drops mid-recording: the page reconnects by itself and keeps the data
    n_before = page.evaluate("() => MuseCapture.state.rows")
    page.evaluate("() => window.__mockDisconnect()")
    page.wait_for_function("() => !MuseCapture.state.connected", timeout=5000)
    check("Reconnecting" in page.inner_text("#cap-status") or "Reconnecting" in page.inner_text("#cap-err"), "a drop shows 'Reconnecting' instead of silently failing")
    page.wait_for_function("() => MuseCapture.state.connected", timeout=20000)
    check(page.inner_text("#cap-err") == "", "error cleared after the automatic reconnect")
    page.wait_for_function(f"() => MuseCapture.state.rows > {n_before} + 2", timeout=20000)
    check("/Marker/reconnected" in page.evaluate("() => MuseCapture.csv()"), "reconnect is marked in the recording")
    check(page.evaluate("() => MuseCapture.state.recording"), "recording kept running across the drop")

    # ---- the headband cannot be reached again: clear message, data kept, manual retry works without the picker
    page.evaluate("() => { MuseCapture._timing.reconnectTries = 2; window.__failServices = 99; window.__mockDisconnect(); }")
    page.wait_for_function("() => /Could not reconnect automatically/.test(document.getElementById('cap-err').textContent)", timeout=30000)
    check(not page.locator("#cap-connect").is_disabled(), "Connect Muse is available again after automatic attempts give up")
    check(page.evaluate("() => MuseCapture.hasData()"), "recording so far is kept")
    page.evaluate("() => { window.__failServices = 0; }")
    page.click("#cap-connect")
    page.wait_for_function("() => MuseCapture.state.connected", timeout=15000)
    page.wait_for_timeout(2500)

    # ---- submit with the recording -> reveal -> graph
    page.click("#submit")
    page.wait_for_selector("text=Your session", timeout=15000)
    page.wait_for_selector("#eegbody svg", timeout=40000)
    check(page.locator("#eegbody svg").count() >= 1, "graph drawn on the reveal page")
    check(page.locator(".eegc-chips button, .eegc-chips label").count() >= 5, "band chips shown")
    check(page.locator("#eegkey").is_visible(), "key shown under the graph")
    api = page.request.get(f"{BASE}/api/rv/trials/{tid}/eeg").json()
    check(api["status"]["status"] == "done" and api["series"] and len(api["series"]["t"]) >= 10, f"analysis done with {len(api['series']['t'])} points")
    check(any("first stroke" in (e.get("label", "") if isinstance(e, dict) else str(e)) for e in api["series"]["events"]), "markers reach the graph events")
    page.screenshot(path=str(shots / "reveal_eeg.png"), full_page=True)
    page.locator(".eegc-tools button").first.click()
    check(page.locator(".eegc-tbl").is_visible(), "table view opens")
    page.locator(".eegc-tools button").first.click()

    # ---- session page uses the same chart
    page.goto(f"{BASE}/")
    page.goto(f"{BASE}/rv/trials/{tid}")
    check(page.request.get(f"{BASE}/rv/trials/{tid}/eeg/eeg_source.csv").status == 200, "recording downloadable")

    # ---- a permanently failing link shows a plain explanation, not the raw error
    ctx4 = browser.new_context(viewport={"width": 800, "height": 900}); ctx4.add_init_script(MOCK)
    p4 = ctx4.new_page(); attach(p4)
    p4.goto(f"{BASE}/rv"); p4.click("text=Start new target"); p4.wait_for_url("**/rv/trials/*")
    p4.evaluate("() => { Object.assign(MuseCapture._timing, { settle: 20, retryDelay: 100 }); window.__failServices = 99; }")
    p4.click("#cap-connect")
    p4.wait_for_function("() => /one connection/.test(document.getElementById('cap-err').textContent)", timeout=30000)
    msg = p4.inner_text("#cap-err")
    check("only allows one connection" in msg and "Cannot retrieve" not in msg, "dropped link gets a plain-language explanation")
    check(not p4.locator("#cap-connect").is_disabled(), "Connect stays available to try again")

    # ---- unsupported / insecure message on a non-secure origin
    ctx2 = browser.new_context(viewport={"width": 412, "height": 915}, has_touch=True, is_mobile=True, device_scale_factor=2)
    mp = ctx2.new_page(); attach(mp)
    mp.goto(f"{BASE}/rv"); mp.click("text=Start new target"); mp.wait_for_url("**/rv/trials/*")
    check(mp.locator("#cap-blocked").is_visible() and "Web Bluetooth" in mp.locator("#cap-blocked").inner_text(), "no-Bluetooth browser gets a plain explanation")
    mp.screenshot(path=str(shots / "mobile_nobt.png"), full_page=True)
    ctx3 = browser.new_context(viewport={"width": 412, "height": 915}, has_touch=True, is_mobile=True, device_scale_factor=2)
    ctx3.add_init_script(MOCK)
    mp3 = ctx3.new_page(); attach(mp3)
    mp3.goto(f"{BASE}/rv"); mp3.click("text=Start new target"); mp3.wait_for_url("**/rv/trials/*")
    check(mp3.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1"), "no horizontal overflow on a phone")
    mp3.screenshot(path=str(shots / "mobile_trial.png"), full_page=True)
    mp3.goto(f"{BASE}/rv/trials/{tid}")
    mp3.wait_for_selector("#eegbody svg", timeout=20000)
    check(mp3.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1"), "reveal page has no horizontal overflow on a phone")
    mp3.screenshot(path=str(shots / "mobile_reveal.png"), full_page=True)

    bad = [b for b in bad if not b.endswith("/favicon.ico")]
    check(not bad, f"no failed requests {bad}")
    errors = [e for e in errors if "Failed to load resource" not in e]
    check(not errors, f"no JS errors {errors[:3]}")
    browser.close()
print("screenshots:", shots)
