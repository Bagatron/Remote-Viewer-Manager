"""Remote-viewing trials: coordinate -> sketch + confidence -> reveal -> self-score.

Protocol (blind): the target is chosen and stored when the trial starts, but its image and
details are only served once the trial has been submitted (and, in judging mode, judged).
The server enforces this; the pages and JSON API never contain the target before then.

    assigned --submit--> revealed --score--> complete
    assigned --submit--> judging --judge--> revealed --score--> complete      (judging mode)
    assigned | judging --abandon--> abandoned
"""
from __future__ import annotations

import io
import json
import random
import re
import shutil
import logging
import uuid
from datetime import datetime, timezone
from math import comb
from pathlib import Path
from urllib.parse import quote

import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from PIL import Image
from prometheus_client import Counter
from sqlalchemy import select

from . import ai_feedback, series, targets, trial_eeg
from .observability import evt
from .version import VERSION
from . import config
from .config import MAX_UPLOAD_MB, TRIALS_DIR
from .db import RVTrial, SessionLocal

router = APIRouter()
log = logging.getLogger("rv.trials")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["app_version"] = VERSION

M_TRIALS = Counter("rv_trials_total", "RV trial events", ["event"])
M_TARGET_FETCH = Counter("rv_target_fetch_total", "Target preparation attempts", ["status"])

MAX_PAGES = 6
MAX_PNG_BYTES = 6 * 1024 * 1024
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
EEG_CAPTURE_MAX = 30 * 1024 * 1024
EEG_FILES = {"eeg_source.csv", "eeg_source.xlsx", "eeg_source.xlsm", "annotated_session.png",
             "intuitive_windows.csv", "processed_timeseries.csv"}
REVEALED = ("revealed", "complete", "abandoned")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _get(db, trial_id: str) -> RVTrial:
    t = db.get(RVTrial, trial_id)
    if t is None:
        raise HTTPException(404, "Trial not found")
    return t


def _used(db) -> tuple[set[str], set[str]]:
    """(source ids already used as targets/decoys, coordinates already issued)."""
    ids: set[str] = set()
    coords: set[str] = set()
    for coord, tj, oj in db.execute(select(RVTrial.coordinate, RVTrial.target_json, RVTrial.options_json)):
        coords.add(coord)
        try:
            ids.add(json.loads(tj).get("source_id", ""))
            ids.update(o.get("source_id", "") for o in json.loads(oj))
        except (ValueError, AttributeError):
            pass
    ids.discard("")
    return ids, coords


# --------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------
def binom_sf(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p), exact."""
    return float(sum(comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k, n + 1)))


def poisson_binom_sf(k: int, ps: list[float]) -> float:
    """P(X >= k) when each trial has its own chance of a hit (e.g. some 1-in-4, some 1-in-2), exact."""
    dist = [1.0]
    for p in ps:
        nxt = [0.0] * (len(dist) + 1)
        for i, q in enumerate(dist):
            nxt[i] += q * (1 - p)
            nxt[i + 1] += q * p
        dist = nxt
    return float(sum(dist[max(k, 0):]))


def compute_stats(trials: list[RVTrial]) -> dict:
    revealed = [t for t in trials if t.status in ("revealed", "complete")]
    done = [t for t in trials if t.status == "complete" and t.accuracy is not None]
    judged = [t for t in revealed if t.judged_correct is not None]
    k, m = sum(1 for t in judged if t.judged_correct), len(judged)
    chances = [1 / max(2, len(t.options)) for t in judged]      # each trial's own chance level (4 options = 25%, 2 = 50%)
    levels = sorted({round(c, 4) for c in chances})
    chance_label = "/".join(f"{c * 100:.0f}%" for c in levels) if levels else None
    if len(levels) > 1:
        chance_label = "mixed " + chance_label
    corr = None
    pairs = [(t.confidence, t.accuracy) for t in done if t.confidence is not None]
    if len(pairs) >= 4:
        a, b = np.array(pairs, dtype=float).T
        if a.std() > 0 and b.std() > 0:
            corr = round(float(np.corrcoef(a, b)[0, 1]), 2)
    cats: dict[str, list[int]] = {}
    for t in done:
        cats.setdefault(t.target.get("category", "?"), []).append(t.accuracy)
    return {
        "n_total": len(trials),
        "n_revealed": len(revealed),
        "n_complete": len(done),
        "mean_confidence": round(float(np.mean([t.confidence for t in revealed if t.confidence is not None])), 1)
        if any(t.confidence is not None for t in revealed) else None,
        "mean_accuracy": round(float(np.mean([t.accuracy for t in done])), 1) if done else None,
        "judged_n": m, "judged_hits": k,
        "judged_rate": round(k / m, 3) if m else None,
        "judged_p": round(poisson_binom_sf(k, chances), 4) if m else None,
        "judged_chance": round(sum(chances) / m, 4) if m else None,       # expected hit rate by luck alone
        "judged_chance_label": chance_label,
        "conf_acc_corr": corr,
        "by_category": sorted(
            ({"category": c, "n": len(v), "mean_accuracy": round(float(np.mean(v)), 1)} for c, v in cats.items()),
            key=lambda r: -r["mean_accuracy"]),
    }


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
def _row(t: RVTrial) -> dict:
    return {
        "id": t.id, "coordinate": t.coordinate, "status": t.status,
        "date": _utc(t.created_at).strftime("%Y-%m-%d %H:%M"),
        "confidence": t.confidence, "accuracy": t.accuracy, "judged_correct": t.judged_correct,
        "judging": t.judging_enabled, "category": t.target.get("category") if t.status in REVEALED else None,
        "thumb": t.status in ("revealed", "complete"),
        "eeg": trial_eeg.source_file(t.dir) is not None,
    }


@router.get("/rv")
def rv_index(request: Request, error: str = ""):
    with SessionLocal() as db:
        trials = list(db.scalars(select(RVTrial).order_by(RVTrial.created_at.desc())))
    open_trial = next((t for t in trials if t.status in ("assigned", "judging")), None)
    return templates.TemplateResponse(request, "rv_index.html", {
        "rows": [_row(t) for t in trials], "stats": compute_stats(trials),
        "error": error[:300], "open_trial": open_trial, "n_opts": config.JUDGING_OPTIONS,
    })


@router.post("/rv/start")
def rv_start(judging: str = Form("")):
    want_judging = bool(judging)
    with SessionLocal() as db:
        used, coords = _used(db)
    try:
        prep = targets.prepare(want_judging, used)
        coordinate = targets.new_coordinate(coords)
    except targets.TargetError as exc:
        M_TARGET_FETCH.labels("failed").inc()
        evt(log, logging.WARNING, "target preparation failed", judging=want_judging, error=str(exc)[:200])
        return RedirectResponse(f"/rv?error={quote(str(exc)[:250])}", status_code=303)
    M_TARGET_FETCH.labels("ok").inc()

    trial_id = uuid.uuid4().hex
    d = TRIALS_DIR / trial_id
    d.mkdir(parents=True, exist_ok=True)
    try:
        (d / "target.jpg").write_bytes(prep.target_jpeg)
        target = prep.target.public()
        target["sha256"] = targets.sha256(prep.target_jpeg)  # commitment: the target can't be swapped later
        options: list[dict] = []
        if want_judging:
            items = [("target.jpg", True, prep.target)]
            for i, (cand, data) in enumerate(prep.decoys, 1):
                (d / f"decoy_{i}.jpg").write_bytes(data)
                items.append((f"decoy_{i}.jpg", False, cand))
            random.SystemRandom().shuffle(items)
            options = [{"key": f"opt{i}", "file": f, "is_target": is_t, "source_id": c.source_id,
                        "category": c.category, "title": c.title, "page_url": c.page_url}
                       for i, (f, is_t, c) in enumerate(items)]
        t = RVTrial(id=trial_id, coordinate=coordinate, judging_enabled=want_judging,
                    target_json=json.dumps(target), options_json=json.dumps(options))
        with SessionLocal() as db:
            db.add(t)
            db.commit()
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise
    M_TRIALS.labels("started").inc()
    evt(log, logging.INFO, "trial started", trial_id=trial_id, coordinate=coordinate, judging=want_judging)   # never the target
    return RedirectResponse(f"/rv/trials/{trial_id}", status_code=303)


@router.get("/rv/trials/{trial_id}")
def rv_trial(request: Request, trial_id: str):
    with SessionLocal() as db:
        t = _get(db, trial_id)
    ctx = {
        "t": t, "created_ms": int(_utc(t.created_at).timestamp() * 1000),
        "pages": [i for i in range(1, MAX_PAGES + 1) if (t.dir / f"sketch_{i}.png").exists()],
        "reveal": t.target if t.status in REVEALED else None,   # nothing about the target before this
        "option_keys": [o["key"] for o in t.options] if t.status == "judging" else [],
        "ai_enabled": ai_feedback.enabled(),
        "n_options": len(t.options) or config.JUDGING_OPTIONS,
    }
    return templates.TemplateResponse(request, "rv_trial.html", ctx)


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------
async def _read_png(f: UploadFile) -> bytes:
    data = await f.read(MAX_PNG_BYTES + 1)
    if len(data) > MAX_PNG_BYTES:
        raise HTTPException(413, "A sketch page is larger than 6 MB.")
    if not data.startswith(PNG_MAGIC):
        raise HTTPException(400, "Sketch pages must be PNG images.")
    try:
        im = Image.open(io.BytesIO(data))
        im.verify()
        if max(im.size) > 4000:
            raise ValueError("too large")
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "Sketch page is not a valid PNG.") from None
    return data


@router.post("/rv/trials/{trial_id}/submit")
async def rv_submit(trial_id: str, confidence: int = Form(...), notes: str = Form(""),
                    pages: list[UploadFile] = File(default=[]), eeg: UploadFile | None = File(default=None)):
    if not 0 <= confidence <= 100:
        raise HTTPException(400, "Confidence must be between 0 and 100.")
    pages = [p for p in pages if p.filename]
    if len(pages) > MAX_PAGES:
        raise HTTPException(400, f"At most {MAX_PAGES} sketch pages.")
    notes = notes.strip()[:20000]
    if not pages and not notes:
        raise HTTPException(400, "Add a sketch or some notes before submitting.")
    blobs = [await _read_png(p) for p in pages]
    eeg_bytes = None
    if eeg is not None and eeg.filename:
        eeg_bytes = await eeg.read(EEG_CAPTURE_MAX + 1)
        if len(eeg_bytes) > EEG_CAPTURE_MAX:
            raise HTTPException(413, "The brainwave recording is larger than 30 MB.")
        if not eeg_bytes.lstrip()[:9].lower().startswith(b"timestamp"):
            raise HTTPException(400, "The brainwave recording is not a valid CSV (first column must be TimeStamp).")
    with SessionLocal() as db:
        t = _get(db, trial_id)
        if t.status != "assigned":
            raise HTTPException(409, "This trial was already submitted.")
        for old in t.dir.glob("sketch_*.png"):
            old.unlink()
        for i, b in enumerate(blobs, 1):
            (t.dir / f"sketch_{i}.png").write_bytes(b)
        now = _now()
        t.confidence, t.notes, t.n_pages = confidence, notes, len(blobs)
        t.submitted_at = now
        t.duration_s = max(0, int((now - _utc(t.created_at)).total_seconds()))
        t.status = "judging" if t.judging_enabled else "revealed"
        db.commit()
        if eeg_bytes:
            trial_eeg.save_source(t.dir, eeg_bytes, ".csv", "bluetooth")
            trial_eeg.submit_processing(t.dir, f"trial {t.coordinate}", trial_eeg.notes_for(t))
        status_after, duration, coord = t.status, t.duration_s, t.coordinate
    M_TRIALS.labels("submitted").inc()
    if eeg_bytes:
        M_TRIALS.labels("submitted_with_eeg").inc()
    evt(log, logging.INFO, "trial submitted", trial_id=trial_id, coordinate=coord, pages=len(blobs), confidence=confidence,
        duration_s=duration, eeg_bytes=len(eeg_bytes) if eeg_bytes else 0, status=status_after)
    return JSONResponse({"next": f"/rv/trials/{trial_id}"})


@router.post("/rv/trials/{trial_id}/judge")
def rv_judge(trial_id: str, choice: str = Form(...)):
    with SessionLocal() as db:
        t = _get(db, trial_id)
        if t.status != "judging":
            raise HTTPException(409, "This trial is not waiting for judging.")
        picked = next((o for o in t.options if o["key"] == choice), None)
        if picked is None:
            raise HTTPException(400, "Pick one of the images shown.")
        t.judged_choice, t.judged_correct, t.status = choice, bool(picked["is_target"]), "revealed"
        db.commit()
    M_TRIALS.labels("judged").inc()
    evt(log, logging.INFO, "trial judged", trial_id=trial_id)
    return RedirectResponse(f"/rv/trials/{trial_id}", status_code=303)


@router.post("/rv/trials/{trial_id}/score")
def rv_score(trial_id: str, accuracy: int = Form(...), feedback_notes: str = Form("")):
    if not 0 <= accuracy <= 100:
        raise HTTPException(400, "Accuracy must be between 0 and 100.")
    with SessionLocal() as db:
        t = _get(db, trial_id)
        if t.status not in ("revealed", "complete"):
            raise HTTPException(409, "Reveal the target before scoring.")
        t.accuracy, t.feedback_notes = accuracy, feedback_notes.strip()[:20000]
        if t.status != "complete":
            t.status, t.completed_at = "complete", _now()
        db.commit()
    M_TRIALS.labels("scored").inc()
    evt(log, logging.INFO, "trial scored", trial_id=trial_id, accuracy=accuracy)
    return RedirectResponse(f"/rv/trials/{trial_id}", status_code=303)


@router.post("/rv/trials/{trial_id}/abandon")
def rv_abandon(trial_id: str):
    with SessionLocal() as db:
        t = _get(db, trial_id)
        if t.status in ("assigned", "judging"):
            t.status = "abandoned"
            db.commit()
            M_TRIALS.labels("abandoned").inc()
            evt(log, logging.INFO, "trial abandoned", trial_id=trial_id)
    return RedirectResponse(f"/rv/trials/{trial_id}", status_code=303)


@router.post("/rv/trials/{trial_id}/delete")
def rv_delete(trial_id: str):
    with SessionLocal() as db:
        t = _get(db, trial_id)
        shutil.rmtree(t.dir, ignore_errors=True)
        db.delete(t)
        db.commit()
    evt(log, logging.INFO, "trial deleted", trial_id=trial_id)
    return RedirectResponse("/rv", status_code=303)


# --------------------------------------------------------------------------
# Brainwave recording (served only after the reveal, like the target)
# --------------------------------------------------------------------------
def _revealed_trial(trial_id: str) -> RVTrial:
    with SessionLocal() as db:
        t = _get(db, trial_id)
    if t.status not in REVEALED:
        raise HTTPException(403, "Brainwave results unlock after the reveal.")
    return t


@router.get("/api/rv/trials/{trial_id}/eeg")
def api_trial_eeg(trial_id: str):
    t = _revealed_trial(trial_id)
    st = trial_eeg.status(t.dir)
    if st is None:
        return {"present": False, "ai_enabled": ai_feedback.enabled()}
    out = {"present": True, "status": st, "ai_enabled": ai_feedback.enabled(), "series": None,
           "ai": None, "files": sorted(f.name for f in [trial_eeg.source_file(t.dir),
                                                          *trial_eeg.out_dir(t.dir).glob("*.png"),
                                                          trial_eeg.out_dir(t.dir) / "intuitive_windows.csv"]
                                       if f is not None and f.exists())}
    if st.get("status") == "done":
        out["series"] = series.build_series(trial_eeg.out_dir(t.dir))
        ai_st = ai_feedback.read_status(trial_eeg.out_dir(t.dir))
        out["ai"] = {"status": ai_st, "html": str(ai_feedback.render_markdown(
            ai_feedback.read_feedback(trial_eeg.out_dir(t.dir))))} if ai_st else None
    return out


@router.get("/rv/trials/{trial_id}/eeg/{filename}")
def rv_eeg_file(trial_id: str, filename: str):
    t = _revealed_trial(trial_id)
    if filename not in EEG_FILES:
        raise HTTPException(404, "File not found")
    p = t.dir / filename if filename.startswith("eeg_source") else trial_eeg.out_dir(t.dir) / filename
    if not p.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(p, filename=f"trial_{t.coordinate}_{filename}" if filename.endswith(".csv") else None)


@router.post("/rv/trials/{trial_id}/eeg/upload")
async def rv_eeg_upload(trial_id: str, file: UploadFile = File(...)):
    """Attach a Muse Monitor / Mind Monitor export recorded during this trial (after the reveal)."""
    t = _revealed_trial(trial_id)
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".csv", ".xlsx", ".xlsm"):
        raise HTTPException(400, "Attach a .csv or .xlsx export.")
    limit = MAX_UPLOAD_MB * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"File is larger than {MAX_UPLOAD_MB} MB.")
    trial_eeg.save_source(t.dir, data, suffix, "upload")
    trial_eeg.submit_processing(t.dir, f"trial {t.coordinate}", trial_eeg.notes_for(t))
    return RedirectResponse(f"/rv/trials/{trial_id}", status_code=303)


@router.post("/rv/trials/{trial_id}/eeg/ai")
def rv_eeg_ai(trial_id: str, focus: str = Form("")):
    t = _revealed_trial(trial_id)
    st = trial_eeg.status(t.dir)
    if not st or st.get("status") != "done":
        raise HTTPException(409, "The recording has not been analyzed yet.")
    if not ai_feedback.enabled():
        raise HTTPException(503, "AI feedback is not configured.")
    trial_eeg.queue_ai(t.dir, trial_eeg.notes_for(t), focus.strip()[:500])
    return RedirectResponse(f"/rv/trials/{trial_id}", status_code=303)


# --------------------------------------------------------------------------
# Images (blind-protocol enforcement lives here)
# --------------------------------------------------------------------------
@router.get("/rv/trials/{trial_id}/img/{key}")
def rv_image(trial_id: str, key: str):
    with SessionLocal() as db:
        t = _get(db, trial_id)
    if key == "target":
        ok, name = t.status in REVEALED, "target.jpg"
    elif re.fullmatch(r"opt[0-3]", key):
        opt = next((o for o in t.options if o["key"] == key), None)
        if opt is None:
            raise HTTPException(404, "No such image")
        ok, name = t.status in ("judging",) + REVEALED, opt["file"]
    else:
        raise HTTPException(404, "No such image")
    if not ok:
        raise HTTPException(403, "Not available yet: finish the session first.")
    path = t.dir / name
    if not path.is_file():
        raise HTTPException(404, "Image missing")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@router.get("/rv/trials/{trial_id}/sketch/{n}")
def rv_sketch(trial_id: str, n: int):
    with SessionLocal() as db:
        t = _get(db, trial_id)
    path = t.dir / f"sketch_{n}.png"
    if not 1 <= n <= MAX_PAGES or not path.is_file():
        raise HTTPException(404, "No such sketch")
    return FileResponse(path, media_type="image/png")


# --------------------------------------------------------------------------
# JSON API
# --------------------------------------------------------------------------
@router.get("/api/rv/trials")
def api_trials():
    with SessionLocal() as db:
        trials = list(db.scalars(select(RVTrial).order_by(RVTrial.created_at.desc())))
    out = []
    for t in trials:
        row = {"id": t.id, "coordinate": t.coordinate, "status": t.status,
               "created_at": _utc(t.created_at).isoformat(), "confidence": t.confidence,
               "accuracy": t.accuracy, "judging": t.judging_enabled, "judged_correct": t.judged_correct,
               "duration_s": t.duration_s, "pages": t.n_pages,
               "eeg": (trial_eeg.status(t.dir) or {}).get("status")}
        if t.status in REVEALED:  # never expose the target earlier
            tg = t.target
            row["target"] = {k: tg.get(k) for k in ("title", "category", "provider", "page_url", "creator", "license")}
        out.append(row)
    return {"stats": compute_stats(trials), "trials": out}


@router.get("/api/rv/targets/probe")
def api_probe():
    """Can this deployment reach the image provider and download a usable image?"""
    return targets.probe()
