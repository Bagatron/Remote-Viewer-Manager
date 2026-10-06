"""Drive the Settings page in headless Chromium. Not part of the pytest run. Run:
    python tests/browser_settings.py [path/to/chromium] [screenshot_dir]
"""
import os
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["RV_DATA_DIR"] = tempfile.mkdtemp(prefix="rvset-")
os.environ["RV_SETTINGS_EDITABLE"] = "true"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from app import config  # noqa: E402
from app.main import app  # noqa: E402
from app.version import VERSION  # noqa: E402
from test_rv_trials import Fake  # noqa: E402

chrome = sys.argv[1] if len(sys.argv) > 1 else None
shots = Path(sys.argv[2] if len(sys.argv) > 2 else tempfile.mkdtemp(prefix="shots-"))
shots.mkdir(parents=True, exist_ok=True)

fake = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=fake.serve_forever, daemon=True).start()
config.WIKIMEDIA_API = f"http://127.0.0.1:{fake.server_port}/w/api.php"
srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
threading.Thread(target=srv.run, daemon=True).start()
while not srv.started:
    time.sleep(0.05)
BASE = f"http://127.0.0.1:{srv.servers[0].sockets[0].getsockname()[1]}"


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        raise SystemExit(1)


with sync_playwright() as p:
    b = p.chromium.launch(executable_path=chrome) if chrome else p.chromium.launch()
    page = b.new_page(viewport={"width": 1100, "height": 1000})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE + "/")
    check(f"v{VERSION}" in page.locator("footer").inner_text(), "home page footer shows the version")
    page.click("header >> text=Settings")
    check(page.url.endswith("/settings"), "Settings link in the header works")
    check(page.locator("#save").is_enabled(), "editable when RV_SETTINGS_EDITABLE=true")

    page.fill("#s_ai_base_url", "http://10.1.2.3:3000/")
    page.fill("#s_ai_model", "llama3.1:8b")
    page.fill("#s_ai_api_key", "sk-browser-secret")
    page.fill("#s_otlp_endpoint", "http://192.168.10.78:4318")
    page.screenshot(path=str(shots / "settings-filled.png"), full_page=True)
    page.click("#save")
    page.wait_for_function("document.getElementById('msg').textContent.includes('Saved')")
    page.wait_for_function("document.getElementById('msg').textContent === ''", timeout=10000)   # page reloaded
    check(page.input_value("#s_ai_base_url") == "http://10.1.2.3:3000", "saved value survives reload (slash trimmed)")
    check("sk-browser-secret" not in page.content(), "API key is not in the page")
    check("set (leave blank to keep)" in page.get_attribute("#s_ai_api_key", "placeholder"), "key shows as set")
    check(page.locator("#restart").is_visible() and "OTLP endpoint" in page.inner_text("#restart"), "restart notice for tracing")
    check("settings" in page.locator(".k", has_text="Model").inner_text(), "source label says settings")

    page.fill("#s_judging_options", "9")
    page.click("#save")
    page.wait_for_function("document.getElementById('msg').className.includes('errmsg')")
    check("between 2 and 4" in page.inner_text("#msg"), "validation error shown")
    page.fill("#s_judging_options", "2")

    page.click("[data-test=images]")
    page.wait_for_function("document.getElementById('t-images').textContent.includes('works') || document.getElementById('t-images').className.includes('errmsg')")
    check("works" in page.inner_text("#t-images"), "image source test passes against the fake provider")
    page.click("[data-test=ai]")
    page.wait_for_function("document.getElementById('t-ai').className.includes('errmsg') || document.getElementById('t-ai').className.includes('okmsg')", timeout=30000)
    check(page.locator("#t-ai.errmsg").count() == 1, "AI test reports an unreachable server clearly")
    page.screenshot(path=str(shots / "settings.png"), full_page=True)

    m = b.new_page(viewport={"width": 390, "height": 800})
    m.goto(BASE + "/settings")
    check(m.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), "no horizontal overflow on a phone")
    m.screenshot(path=str(shots / "settings-phone.png"), full_page=True)
    check(not errors, f"no JS errors {errors}")
    b.close()
print("screenshots:", shots)
