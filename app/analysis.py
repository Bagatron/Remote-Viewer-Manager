"""Core EEG analysis for remote-viewing sessions.

Ported from the original rv_analyzer.py CLI script. Changes from the original:
  * Accepts Muse Monitor / Mind Monitor CSV exports directly (per-channel band
    columns are averaged into Delta/Theta/Alpha/Beta/Gamma), as well as the
    original Excel layout (GraphingDataAve / Events / Feedback sheets).
  * Uses matplotlib's object-oriented API (no pyplot global state) so it is safe
    to call from web-server worker threads.
  * Returns data structures instead of writing files, so the web layer decides
    where results are stored.
"""
from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
from matplotlib.figure import Figure  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from .digest import build_digest  # noqa: E402

BANDS = ["Delta", "Theta", "Alpha", "Beta", "Gamma"]
CHANNELS = ["TP9", "AF7", "AF8", "TP10"]

# Generic fallback only. These used to be pasted under every window; the session page now
# shows them only when AI feedback is not configured or has failed.
COACHING_TIPS = [
    "Pause here to sketch first impressions.",
    "Limit inner dialogue; breathe 3 slow cycles to stabilize Theta.",
    "Glance at target cue, then close eyes and let imagery form for ~20-30s.",
    "If image fades, switch to sensory probes (texture, temperature, geometry).",
]


@dataclass
class Params:
    window: int = 7
    top_n: int = 3
    min_gap: int = 20
    data_sheet: str = "GraphingDataAve"
    events_sheet: str = "Events"
    feedback_sheet: str = "Feedback"

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Loaded:
    df: pd.DataFrame
    events: pd.DataFrame | None = None
    feedback: pd.DataFrame | None = None
    source_format: str = ""
    quality: dict = field(default_factory=dict)  # blink/jaw/headband/HSI stats when the export has them


@dataclass
class Result:
    df: pd.DataFrame
    windows: pd.DataFrame
    events: pd.DataFrame | None
    threshold: dict | None
    recommended: pd.DataFrame | None
    metrics: dict = field(default_factory=dict)
    summary_text: str = ""
    source_format: str = ""
    digest: dict = field(default_factory=dict)  # compact facts handed to the AI coach


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
def _band_from_channels(df: pd.DataFrame, band: str) -> pd.Series | None:
    cols = [f"{band}_{ch}" for ch in CHANNELS if f"{band}_{ch}" in df.columns]
    if not cols:
        return None
    # Mind Monitor leaves band columns blank on non-band rows; mean skips NaN.
    return df[cols].apply(pd.to_numeric, errors="coerce").mean(axis=1, skipna=True)


def _csv_quality(raw: pd.DataFrame) -> dict:
    """Artifact / contact-quality stats from optional Muse Monitor / Mind Monitor columns."""
    q: dict = {}
    if "Elements" in raw.columns:
        el = raw["Elements"].dropna().astype(str).str.lower()
        q["blinks"] = int(el.str.contains("blink").sum())
        q["jaw_clenches"] = int(el.str.contains("jaw_clench").sum())
    if "HeadBandOn" in raw.columns:
        hb = pd.to_numeric(raw["HeadBandOn"], errors="coerce").dropna()
        if len(hb):
            q["headband_off_fraction"] = float((hb < 0.5).mean())
    hsi = {}
    for ch in CHANNELS:
        col = f"HSI_{ch}"
        if col in raw.columns:
            v = pd.to_numeric(raw[col], errors="coerce").dropna()
            if len(v):
                hsi[ch] = float(v.mean())
    if hsi:
        q["hsi_mean"] = hsi  # 1 = good contact ... 4 = bad
    return q


def _load_csv(path: Path) -> Loaded:
    raw = pd.read_csv(path, low_memory=False)
    if "TimeStamp" not in raw.columns:
        raise ValueError("Expected a 'TimeStamp' column in the CSV.")
    raw["TimeStamp"] = pd.to_datetime(raw["TimeStamp"], errors="coerce")
    raw = raw.dropna(subset=["TimeStamp"]).sort_values("TimeStamp").reset_index(drop=True)

    events = None
    if "Elements" in raw.columns:
        el = raw[raw["Elements"].notna()][["TimeStamp", "Elements"]].copy()
        el["Elements"] = el["Elements"].astype(str)
        # Keep user markers; drop the device's own housekeeping elements.
        el = el[el["Elements"].str.contains("marker", case=False)]
        if len(el):
            events = el.rename(columns={"Elements": "Label"}).reset_index(drop=True)

    out = pd.DataFrame({"TimeStamp": raw["TimeStamp"]})
    for band in BANDS:
        if band in raw.columns:  # already-averaged export
            out[band] = pd.to_numeric(raw[band], errors="coerce")
        else:
            series = _band_from_channels(raw, band)
            if series is None:
                raise ValueError(
                    f"Missing band data for {band}: need '{band}' or "
                    f"'{band}_TP9/AF7/AF8/TP10' columns."
                )
            out[band] = series
    out = out.dropna(subset=BANDS, how="all")
    out = out.dropna(subset=BANDS).reset_index(drop=True)
    if len(out) < 3:
        raise ValueError("Not enough band-power rows in file to analyze.")
    return Loaded(df=out, events=events, source_format="csv", quality=_csv_quality(raw))


def _load_excel(path: Path, p: Params) -> Loaded:
    xls = pd.ExcelFile(path)
    if p.data_sheet not in xls.sheet_names:
        raise ValueError(
            f"Sheet '{p.data_sheet}' not found. Sheets: {', '.join(xls.sheet_names)}"
        )
    df = pd.read_excel(xls, sheet_name=p.data_sheet)
    if "TimeStamp" not in df.columns:
        raise ValueError("Expected a 'TimeStamp' column in data sheet.")
    df["TimeStamp"] = pd.to_datetime(df["TimeStamp"])
    df = df.sort_values("TimeStamp").reset_index(drop=True)
    missing = [b for b in BANDS if b not in df.columns]
    if missing:
        raise ValueError(f"Missing column(s): {', '.join(missing)}")
    return Loaded(
        df=df,
        events=_load_events(xls, p.events_sheet),
        feedback=_load_feedback(xls, p.feedback_sheet),
        source_format="xlsx",
    )


def load_file(path: str | Path, params: Params) -> Loaded:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return _load_csv(path)
    if suffix in (".xlsx", ".xlsm"):
        return _load_excel(path, params)
    raise ValueError(f"Unsupported file type '{suffix}'. Upload .csv or .xlsx.")


def _load_events(xls: pd.ExcelFile, sheet: str):
    if sheet in xls.sheet_names:
        ev = pd.read_excel(xls, sheet_name=sheet)
        if "TimeStamp" in ev.columns:
            ev["TimeStamp"] = pd.to_datetime(ev["TimeStamp"])
            if "Label" not in ev.columns:
                ev["Label"] = ""
            return ev
    return None


def _load_feedback(xls: pd.ExcelFile, sheet: str):
    if sheet not in xls.sheet_names:
        return None
    fb = pd.read_excel(xls, sheet_name=sheet)
    cols = {c.lower(): c for c in fb.columns}
    label = fb[cols["label"]].astype(str) if "label" in cols else ""
    if "timestamp" in cols and "score" in cols:
        out = pd.DataFrame(
            {
                "Start": pd.to_datetime(fb[cols["timestamp"]]),
                "Score": pd.to_numeric(fb[cols["score"]], errors="coerce"),
                "Label": label,
            }
        )
        out["End"] = out["Start"]
        out["is_interval"] = False
        return out.dropna(subset=["Start", "Score"])
    if {"start", "end", "score"} <= set(cols):
        out = pd.DataFrame(
            {
                "Start": pd.to_datetime(fb[cols["start"]]),
                "End": pd.to_datetime(fb[cols["end"]]),
                "Score": pd.to_numeric(fb[cols["score"]], errors="coerce"),
                "Label": label,
            }
        )
        out["is_interval"] = True
        return out.dropna(subset=["Start", "End", "Score"])
    return None


# --------------------------------------------------------------------------
# Processing
# --------------------------------------------------------------------------
def smooth_bands(df: pd.DataFrame, window: int) -> pd.DataFrame:
    df = df.copy()
    for col in BANDS:
        df[f"{col}_sm"] = df[col].rolling(window, center=True, min_periods=1).mean()
    return df


def compute_rv_index(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["RV_index"] = df["Theta_sm"] - df["Beta_sm"]
    std = df["RV_index"].std()
    if std == 0 or np.isnan(std):
        std = 1.0
    df["RV_index_z"] = (df["RV_index"] - df["RV_index"].mean()) / std
    return df


def align_feedback(df: pd.DataFrame, fb: pd.DataFrame | None) -> pd.DataFrame:
    df = df.copy()
    df["Feedback"] = np.nan
    if fb is None or len(fb) == 0:
        return df
    if bool(fb["is_interval"].iloc[0]):
        for _, row in fb.iterrows():
            mask = (df["TimeStamp"] >= row["Start"]) & (df["TimeStamp"] <= row["End"])
            df.loc[mask, "Feedback"] = row["Score"]
    else:
        for _, row in fb.iterrows():
            idx = (df["TimeStamp"] - row["Start"]).abs().argmin()
            df.loc[idx, "Feedback"] = row["Score"]
    return df


def detect_windows(df: pd.DataFrame, top_n: int, min_gap_sec: int) -> pd.DataFrame:
    vals = df["RV_index_z"].values
    candidates = [i for i in range(1, len(vals) - 1) if vals[i] > vals[i - 1] and vals[i] > vals[i + 1]]
    ranked = sorted(candidates, key=lambda i: vals[i], reverse=True)
    secs = (df["TimeStamp"] - df["TimeStamp"].iloc[0]).dt.total_seconds().values
    selected: list[int] = []
    for i in ranked:
        if len(selected) >= top_n:
            break
        if all(abs(secs[i] - secs[j]) >= min_gap_sec for j in selected):
            selected.append(i)
    annot = df.iloc[selected][["TimeStamp", "RV_index_z", "Theta_sm", "Alpha_sm", "Beta_sm"]].copy()
    annot["Window"] = range(1, len(annot) + 1)
    return annot.reset_index(drop=True)


def optimize_threshold(df: pd.DataFrame, steps: int = 40) -> dict | None:
    sub = df.dropna(subset=["Feedback"])
    if len(sub) < 10:
        return None
    y = (sub["Feedback"] > 0.5).astype(int).values
    x = sub["RV_index_z"].values
    best = {"thr": None, "j": -999.0, "tpr": 0.0, "fpr": 1.0}
    for t in np.linspace(np.nanmin(x), np.nanmax(x), steps):
        pred = (x >= t).astype(int)
        tp = int(np.sum((pred == 1) & (y == 1)))
        fp = int(np.sum((pred == 1) & (y == 0)))
        fn = int(np.sum((pred == 0) & (y == 1)))
        tn = int(np.sum((pred == 0) & (y == 0)))
        tpr = tp / (tp + fn) if (tp + fn) else 0.0
        fpr = fp / (fp + tn) if (fp + tn) else 0.0
        j = tpr - fpr
        if j > best["j"]:
            best = {"thr": float(t), "j": float(j), "tpr": float(tpr), "fpr": float(fpr)}
    return best


def analyze(loaded: Loaded, params: Params, name: str) -> Result:
    df = compute_rv_index(smooth_bands(loaded.df, params.window))
    windows = detect_windows(df, params.top_n, params.min_gap)
    df_fb = align_feedback(df, loaded.feedback)
    thr = optimize_threshold(df_fb)

    recommended = None
    if thr is not None:
        recommended = df_fb[df_fb["RV_index_z"] >= thr["thr"]][["TimeStamp", "RV_index_z"]]

    duration = float((df["TimeStamp"].iloc[-1] - df["TimeStamp"].iloc[0]).total_seconds())
    metrics = {
        "rows": int(len(df)),
        "duration_sec": duration,
        "started_at": df["TimeStamp"].iloc[0].isoformat(),
        "rv_mean": float(df["RV_index_z"].mean()),  # ~0 by construction (z-scored per session)
        "rv_raw_mean": float(df["RV_index"].mean()),  # Theta - Beta, comparable across sessions
        "rv_max": float(df["RV_index_z"].max()),
        "theta_mean": float(df["Theta_sm"].mean()),
        "beta_mean": float(df["Beta_sm"].mean()),
        "alpha_mean": float(df["Alpha_sm"].mean()),
        "n_events": int(len(loaded.events)) if loaded.events is not None else 0,
        "n_windows": int(len(windows)),
        "threshold": thr,
    }

    digest = build_digest(df_fb, windows, loaded.events, loaded.quality, thr, name,
                          loaded.source_format, params.to_dict())
    metrics["quality_flags"] = digest["quality"]["flags"]

    lines = [f"Session: {name}", f"Data points: {len(df)}", "Top windows:"]
    for _, row in windows.iterrows():
        lines.append(f" - {row['TimeStamp']} | RV_index_z={row['RV_index_z']:.2f}")
    lines.append("")
    if thr is not None:
        lines += [
            "Personal signature (optimized on your feedback):",
            f" - Best RV_index_z threshold ~ {thr['thr']:.2f} (Youden J={thr['j']:.2f})",
            f" - Sensitivity (TPR)={thr['tpr']:.2f}, 1-Specificity (FPR)={thr['fpr']:.2f}",
        ]
    else:
        lines.append("No usable feedback (need >= 10 scored samples); using top windows as coaching anchors.")

    return Result(
        df=df_fb,  # includes the aligned Feedback column (NaN when no feedback)
        windows=windows,
        events=loaded.events,
        threshold=thr,
        recommended=recommended,
        metrics=metrics,
        summary_text="\n".join(lines),
        source_format=loaded.source_format,
        digest=digest,
    )


# --------------------------------------------------------------------------
# Plots
# --------------------------------------------------------------------------
def _png_bytes(fig: Figure) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=160, bbox_inches="tight")
    return buf.getvalue()


def plot_session(res: Result) -> bytes:
    df, windows, events = res.df, res.windows, res.events
    fig = Figure(figsize=(14, 6))
    ax = fig.subplots()
    ax.plot(df["TimeStamp"], df["Alpha_sm"], label="Alpha (smoothed)")
    ax.plot(df["TimeStamp"], df["Theta_sm"], label="Theta (smoothed)")
    ax.plot(df["TimeStamp"], df["Beta_sm"], label="Beta (smoothed)")
    ax.plot(df["TimeStamp"], df["RV_index_z"], label="RV Index (Theta-Beta, z)", linestyle="--")
    for _, row in windows.iterrows():
        ts, y = row["TimeStamp"], row["RV_index_z"]
        ax.scatter([ts], [y], zorder=5)
        ax.annotate(
            f"Window {int(row['Window'])}\n{ts.strftime('%H:%M:%S')}",
            (ts, y), textcoords="offset points", xytext=(0, 10), ha="center",
        )
    if events is not None and len(events) > 0:
        ytop = ax.get_ylim()[1]
        for _, e in events.iterrows():
            ax.axvline(e["TimeStamp"], linestyle=":", linewidth=1)
            ax.text(e["TimeStamp"], ytop * 0.9, str(e["Label"]), rotation=90, va="top", ha="right")
    ax.set_title("Remote Viewing: Bands + RV Index + Annotated Windows")
    ax.set_xlabel("Time")
    ax.set_ylabel("Amplitude / z-score")
    ax.margins(y=0.15)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=4, frameon=False)
    fig.autofmt_xdate()
    return _png_bytes(fig)


def plot_feedback_scatter(res: Result) -> bytes | None:
    """Scatter of RV index vs. feedback score (the README promised this; the
    original script never produced it)."""
    df = res.df
    if "Feedback" not in df.columns:
        return None
    sub = df.dropna(subset=["Feedback"])
    if len(sub) < 3:
        return None
    fig = Figure(figsize=(7, 5))
    ax = fig.subplots()
    ax.scatter(sub["RV_index_z"], sub["Feedback"], alpha=0.7)
    if res.threshold:
        ax.axvline(res.threshold["thr"], linestyle="--", label=f"threshold {res.threshold['thr']:.2f}")
        ax.legend()
    ax.set_xlabel("RV Index (z)")
    ax.set_ylabel("Feedback score")
    ax.set_title("RV Index vs. Feedback")
    return _png_bytes(fig)


def plot_trend(summaries: list[dict], max_points: int = 60) -> bytes | None:
    """summaries: [{'name': str, 'rv_raw_mean': float, 'kind': 'session'|'trial'}, ...] in chronological order.

    Plots the mean raw Theta-Beta per recording. Uploaded sessions and recordings captured inside RV trials
    share one time axis, drawn with different markers. (The original script plotted the mean of the
    per-session z-score, which is ~0 for every session and so shows nothing.)
    """
    if not summaries:
        return None
    summaries = summaries[-max_points:]
    kinds = {"session": ("Uploaded session", "o", "#2563eb"), "trial": ("RV trial (captured here)", "D", "#d9822b")}
    fig = Figure(figsize=(10, 4.2))
    ax = fig.subplots()
    ax.plot(range(len(summaries)), [x["rv_raw_mean"] for x in summaries], color="#9aa5b4", lw=1, zorder=1)
    for kind, (label, marker, color) in kinds.items():
        pts = [(i, x["rv_raw_mean"]) for i, x in enumerate(summaries) if x.get("kind", "session") == kind]
        if pts:
            ax.scatter([i for i, _ in pts], [v for _, v in pts], marker=marker, s=46, color=color, label=label,
                       zorder=3, edgecolor="white", linewidth=0.8)
    ax.set_xticks(range(len(summaries)))
    ax.set_xticklabels([x["name"] for x in summaries], rotation=45, ha="right", fontsize=8 if len(summaries) > 20 else 10)
    ax.set_title("Trend: Mean RV Index (Theta - Beta) per recording")
    ax.set_xlabel("Recording, oldest to newest")
    ax.set_ylabel("Mean Theta - Beta")
    ax.grid(True, alpha=0.25)
    if len({x.get("kind", "session") for x in summaries}) > 1 or any(x.get("kind") == "trial" for x in summaries):
        ax.legend(loc="best", frameon=False)
    return _png_bytes(fig)


# --------------------------------------------------------------------------
# Convenience: run everything and write artifacts into a directory
# --------------------------------------------------------------------------
def run_and_store(src: str | Path, out_dir: str | Path, params: Params, name: str) -> dict:
    """Analyze `src`, write all artifacts to `out_dir`, return metrics dict."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    res = analyze(load_file(src, params), params, name)

    res.windows.to_csv(out / "intuitive_windows.csv", index=False)
    (out / "annotated_session.png").write_bytes(plot_session(res))
    scatter = plot_feedback_scatter(res)
    if scatter:
        (out / "rvindex_feedback_scatter.png").write_bytes(scatter)
    if res.recommended is not None:
        res.recommended.to_csv(out / "recommended_moments.csv", index=False)
    # Processed time series so it can be re-plotted / queried later without the raw file.
    cols = ["TimeStamp"] + [f"{b}_sm" for b in BANDS] + ["RV_index", "RV_index_z"]
    res.df[cols].to_csv(out / "processed_timeseries.csv", index=False)
    if res.events is not None and len(res.events):
        ev = res.events[["TimeStamp", "Label"]].copy()
        ev.to_csv(out / "events.csv", index=False)
    (out / "coaching_summary.txt").write_text(res.summary_text)
    (out / "digest_base.json").write_text(json.dumps(res.digest, indent=2))
    (out / "metrics.json").write_text(json.dumps(res.metrics, indent=2))
    return res.metrics
