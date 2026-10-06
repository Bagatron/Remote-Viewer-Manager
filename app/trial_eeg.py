"""Brainwave recordings attached to RV trials.

A recording arrives either from the browser's Bluetooth capture during the trial (a CSV in the Muse
Monitor column layout, written by static/muse.js) or as a Muse Monitor / Mind Monitor file attached
afterwards. Both go through the same analysis as an EEG session. State lives in plain files inside the
trial folder, so no database migration is needed:

    eeg_source.csv|xlsx   what was uploaded
    eeg_status.json       {status: pending|done|failed, error, rows, source, ...}
    eeg_out/              graphs, windows, processed series, digest (analysis.run_and_store output)
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from prometheus_client import Counter

from . import ai_feedback, analysis, config, tracing

log = logging.getLogger("rv.eeg")
M_EEG = Counter("rv_trial_eeg_total", "Trial EEG recordings processed", ["status"])

pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rv-trial-eeg")
_ai_executor: ThreadPoolExecutor | None = None   # the shared one-GPU AI worker, set by main.py

CAPTURE_WINDOW_S = 5          # smoothing in seconds for recordings made at ~1 Hz / any rate
OUT = "eeg_out"


def set_ai_executor(ex: ThreadPoolExecutor) -> None:
    global _ai_executor
    _ai_executor = ex


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---- state ---------------------------------------------------------------------------------
def status(trial_dir: Path) -> dict | None:
    try:
        return json.loads((Path(trial_dir) / "eeg_status.json").read_text())
    except (OSError, ValueError):
        return None


def _write(trial_dir: Path, **fields) -> dict:
    cur = status(trial_dir) or {}
    cur.update(fields)
    (Path(trial_dir) / "eeg_status.json").write_text(json.dumps(cur, indent=2))
    return cur


def source_file(trial_dir: Path) -> Path | None:
    for ext in (".csv", ".xlsx", ".xlsm"):
        p = Path(trial_dir) / f"eeg_source{ext}"
        if p.exists():
            return p
    return None


def out_dir(trial_dir: Path) -> Path:
    return Path(trial_dir) / OUT


# ---- saving + processing -------------------------------------------------------------------
def save_source(trial_dir: Path, data: bytes, suffix: str, origin: str) -> None:
    """Replace any earlier recording with this one and mark it pending."""
    d = Path(trial_dir)
    for old in list(d.glob("eeg_source.*")):
        old.unlink()
    shutil.rmtree(out_dir(d), ignore_errors=True)
    (d / f"eeg_source{suffix}").write_bytes(data)
    (d / "eeg_status.json").write_text(json.dumps(
        {"status": "pending", "origin": origin, "error": None, "queued_at": _now()}, indent=2))


def _params_for(src: Path) -> analysis.Params:
    """Pick a smoothing window of about CAPTURE_WINDOW_S seconds whatever the sample rate."""
    window = analysis.Params().window
    try:
        if src.suffix.lower() == ".csv":
            ts = pd.to_datetime(pd.read_csv(src, usecols=["TimeStamp"])["TimeStamp"], errors="coerce").dropna()
            if len(ts) > 3:
                dt = ts.diff().dt.total_seconds().median()
                if dt and dt > 0:
                    window = max(1, min(60, round(CAPTURE_WINDOW_S / dt)))
    except Exception:  # noqa: BLE001 - fall back to the default
        pass
    return analysis.Params(window=window)


def process(trial_dir: Path, trial_name: str, notes: str, run_ai: bool = True) -> None:
    """Blocking: analyze the stored recording and (optionally) queue the AI coach."""
    d = Path(trial_dir)
    src = source_file(d)
    started = time.perf_counter()
    if src is None:
        _write(d, status="failed", error="No recording found.")
        return
    try:
        params = _params_for(src)
        with tracing.span("trial_eeg.analysis"):
            metrics = analysis.run_and_store(src, out_dir(d), params, trial_name)
        _write(d, status="done", error=None, rows=metrics.get("rows"),
               duration_s=round(time.perf_counter() - started, 1), window=params.window, finished_at=_now())
        M_EEG.labels("done").inc()
        log.info("trial EEG analysis finished", extra={"fields": {
            "trial_dir": d.name, "rows": metrics.get("rows"), "duration_s": round(time.perf_counter() - started, 1)}})
    except Exception as exc:  # noqa: BLE001 - shown on the page, never blocks the trial
        log.warning("trial EEG analysis failed for %s: %s", d.name, exc, extra={"fields": {"trial_dir": d.name}})
        _write(d, status="failed", error=str(exc), finished_at=_now())
        M_EEG.labels("failed").inc()
        return
    if run_ai and config.AI_AUTO and ai_feedback.enabled() and _ai_executor is not None:
        queue_ai(d, notes)


def queue_ai(trial_dir: Path, notes: str, focus: str = "") -> None:
    od = out_dir(trial_dir)
    ai_feedback.mark_pending(od, focus)
    (Path(trial_dir) / "eeg_ai_notes.txt").write_text(notes)
    if _ai_executor is not None:
        _ai_executor.submit(_run_ai, Path(trial_dir))


def _run_ai(trial_dir: Path) -> None:
    try:
        notes = (Path(trial_dir) / "eeg_ai_notes.txt").read_text()
    except OSError:
        notes = ""
    ai_feedback.generate(out_dir(trial_dir), notes, [])


def submit_processing(trial_dir: Path, trial_name: str, notes: str) -> None:
    pool.submit(tracing.bind(process), Path(trial_dir), trial_name, notes)


def notes_for(trial) -> str:
    """What the person told us about the trial; never includes anything about the target."""
    parts = ["This recording is from a blind remote-viewing trial (the target is not shown to you)."]
    if trial.notes:
        parts.append("Impressions written during the session: " + trial.notes.strip()[:700])
    if trial.confidence is not None:
        parts.append(f"Confidence entered before the reveal: {trial.confidence}/100.")
    if trial.accuracy is not None:
        parts.append(f"Self-scored match after the reveal: {trial.accuracy}/100. "
                     "Remember this score is subjective and says nothing about what the EEG shows.")
    if trial.feedback_notes:
        parts.append("Reflection after the reveal: " + trial.feedback_notes.strip()[:500])
    return "\n".join(parts)


def resume(trials) -> None:
    """On startup, re-run anything that was queued when the pod last stopped."""
    for t in trials:
        st = status(t.dir)
        if st and st.get("status") == "pending" and source_file(t.dir):
            pool.submit(tracing.bind(process), t.dir, f"trial {t.coordinate}", notes_for(t))
        elif st and st.get("status") == "done" and ai_feedback.is_pending(out_dir(t.dir)) and _ai_executor:
            _ai_executor.submit(_run_ai, t.dir)
