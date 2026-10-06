"""Turn a finished analysis directory into compact JSON for the interactive graph.

Works for both EEG sessions and trial recordings: both are written by analysis.run_and_store,
so the same files exist (processed_timeseries.csv, intuitive_windows.csv, events.csv, metrics.json).
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

BANDS = ["Delta", "Theta", "Alpha", "Beta", "Gamma"]
MAX_POINTS = 1200


def _strip_marker(label: str) -> str:
    s = str(label).strip()
    low = s.lower()
    i = low.find("/marker/")
    if i >= 0:
        s = s[i + len("/marker/"):]
    return s.strip(" /") or "marker"


def build_series(d: Path) -> dict | None:
    d = Path(d)
    ts_path = d / "processed_timeseries.csv"
    if not ts_path.exists():
        return None
    df = pd.read_csv(ts_path, parse_dates=["TimeStamp"])
    if len(df) < 2:
        return None
    t0 = df["TimeStamp"].iloc[0]
    df["t"] = (df["TimeStamp"] - t0).dt.total_seconds()
    duration = float(df["t"].iloc[-1])
    hz = (len(df) - 1) / duration if duration > 0 else 1.0

    # Thin long recordings (e.g. 10 Hz exports) by averaging buckets; short ones are left alone.
    if len(df) > MAX_POINTS:
        stride = -(-len(df) // MAX_POINTS)
        df = df.groupby(df.index // stride).mean(numeric_only=True)

    def col(name):
        return [None if pd.isna(v) else round(float(v), 4) for v in df[name]]

    out: dict = {
        "duration_s": round(duration, 1),
        "hz": round(hz, 2),
        "t": [round(float(v), 2) for v in df["t"]],
        "bands": {b: col(f"{b}_sm") for b in BANDS if f"{b}_sm" in df.columns},
        "rv": col("RV_index"),
        "rv_z": col("RV_index_z"),
        "events": [],
        "windows": [],
        "flags": [],
    }
    ev_path = d / "events.csv"
    if ev_path.exists():
        ev = pd.read_csv(ev_path, parse_dates=["TimeStamp"])
        for _, r in ev.iterrows():
            sec = (r["TimeStamp"] - t0).total_seconds()
            if -1 <= sec <= duration + 1:
                out["events"].append({"t": round(max(0.0, sec), 2), "label": _strip_marker(r["Label"])})
        out["events"].sort(key=lambda e: e["t"])
    win_path = d / "intuitive_windows.csv"
    if win_path.exists():
        w = pd.read_csv(win_path, parse_dates=["TimeStamp"])
        for _, r in w.iterrows():
            out["windows"].append({
                "n": int(r["Window"]), "t": round(float((r["TimeStamp"] - t0).total_seconds()), 2),
                "rv_z": round(float(r["RV_index_z"]), 3),
            })
    try:
        m = json.loads((d / "metrics.json").read_text())
        out["flags"] = list((m.get("quality_flags") or []))
        out["threshold"] = m.get("threshold")
    except (OSError, ValueError):
        pass
    return out
