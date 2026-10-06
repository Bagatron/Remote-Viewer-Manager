"""Deterministic session digest.

The numbers the AI coach reasons over are computed here, in plain pandas/numpy, so they
are reproducible and checkable. The language model never sees raw EEG, only this compact
summary (a few hundred numbers), which keeps prompts small enough for a 7-8B model on a
6 GB GPU and keeps the model from inventing measurements.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

BANDS = ["Delta", "Theta", "Alpha", "Beta", "Gamma"]


def _r(x, n: int = 3):
    """Round to JSON-safe float (None for NaN/inf)."""
    if x is None:
        return None
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return None
    return round(xf, n) if np.isfinite(xf) else None


def _mmss(sec: float) -> str:
    sec = max(0, int(round(sec)))
    return f"{sec // 60:02d}:{sec % 60:02d}"


def _slope_per_min(secs: np.ndarray, y: np.ndarray) -> float | None:
    ok = np.isfinite(y)
    if ok.sum() < 10 or np.ptp(secs[ok]) <= 0:
        return None
    return _r(np.polyfit(secs[ok], y[ok], 1)[0] * 60.0, 4)


def _window_mean(secs: np.ndarray, y: np.ndarray, lo: float, hi: float):
    m = (secs >= lo) & (secs < hi)
    return float(np.nanmean(y[m])) if m.any() else None


def build_digest(
    df: pd.DataFrame,
    windows: pd.DataFrame,
    events: pd.DataFrame | None,
    quality: dict | None,
    threshold: dict | None,
    name: str,
    source_format: str,
    params: dict,
) -> dict:
    t0 = df["TimeStamp"].iloc[0]
    secs = (df["TimeStamp"] - t0).dt.total_seconds().to_numpy()
    dur = float(secs[-1])
    dt = np.diff(secs)
    med_dt = float(np.median(dt)) if len(dt) else None
    z = df["RV_index_z"].to_numpy(dtype=float)
    rv = df["RV_index"].to_numpy(dtype=float)

    # ---- session / baseline ------------------------------------------------
    baseline = {}
    for b in BANDS:
        s = df[f"{b}_sm"]
        baseline[b.lower()] = {
            "mean": _r(s.mean()), "std": _r(s.std()),
            "p10": _r(s.quantile(0.10)), "p90": _r(s.quantile(0.90)),
        }
    baseline["theta_minus_beta"] = {"mean": _r(rv.mean()), "std": _r(rv.std())}

    # ---- coarse timeline ---------------------------------------------------
    n_seg = int(min(10, max(3, dur // 30)))
    edges = np.linspace(0, dur, n_seg + 1)
    timeline = []
    for i in range(n_seg):
        hi = edges[i + 1] + (1e-9 if i == n_seg - 1 else 0)
        m = (secs >= edges[i]) & (secs < hi)
        if not m.any():
            continue
        timeline.append({
            "span": f"{_mmss(edges[i])}-{_mmss(edges[i + 1])}",
            "theta": _r(df["Theta_sm"].to_numpy()[m].mean()),
            "alpha": _r(df["Alpha_sm"].to_numpy()[m].mean()),
            "beta": _r(df["Beta_sm"].to_numpy()[m].mean()),
            "theta_minus_beta": _r(rv[m].mean()),
        })
    trend = {
        "theta_minus_beta_per_min": _slope_per_min(secs, rv),
        "theta_per_min": _slope_per_min(secs, df["Theta_sm"].to_numpy(dtype=float)),
        "alpha_per_min": _slope_per_min(secs, df["Alpha_sm"].to_numpy(dtype=float)),
        "beta_per_min": _slope_per_min(secs, df["Beta_sm"].to_numpy(dtype=float)),
    }

    # ---- events (markers) --------------------------------------------------
    ev_rows = []
    ev_secs = []
    if events is not None and len(events):
        for _, e in events.head(12).iterrows():
            te = float((e["TimeStamp"] - t0).total_seconds())
            ev_secs.append((te, str(e.get("Label", ""))))
            row = {"label": str(e.get("Label", ""))[:60], "t": _mmss(te)}
            row["rv_z_before_15s"] = _r(_window_mean(secs, z, te - 15, te), 2)
            row["rv_z_after_15s"] = _r(_window_mean(secs, z, te, te + 15), 2)
            for b in ("Theta", "Alpha", "Beta"):
                col = df[f"{b}_sm"].to_numpy(dtype=float)
                a, c = _window_mean(secs, col, te - 15, te), _window_mean(secs, col, te, te + 15)
                row[f"{b.lower()}_change"] = _r(c - a) if a is not None and c is not None else None
            ev_rows.append(row)

    # ---- peak windows ------------------------------------------------------
    peaks = []
    for _, w in windows.iterrows():
        tp = float((w["TimeStamp"] - t0).total_seconds())
        zp = float(w["RV_index_z"])
        prior = _window_mean(secs, z, tp - 30, tp - 5)
        post = _window_mean(secs, z, tp + 5, tp + 30)
        sustained = None
        if zp > 0 and med_dt:
            m = (secs >= tp - 30) & (secs <= tp + 30) & (z >= 0.5 * zp)
            sustained = round(float(m.sum() * med_dt), 1)
        near = None
        if ev_secs:
            te, lab = min(ev_secs, key=lambda x: abs(x[0] - tp))
            near = {"label": lab[:60], "offset_s": round(tp - te, 1)}
        peaks.append({
            "window": int(w["Window"]), "t": _mmss(tp), "rv_z": _r(zp, 2),
            "theta": _r(w["Theta_sm"]), "alpha": _r(w["Alpha_sm"]), "beta": _r(w["Beta_sm"]),
            "rise_vs_prior_30s": _r(zp - prior, 2) if prior is not None else None,
            "post_30s_vs_peak": _r(post - zp, 2) if post is not None else None,
            "seconds_above_half_peak_within_pm30s": sustained,
            "nearest_marker": near,
        })
    peak_context = {
        "fraction_time_z_above_1": _r((z > 1).mean(), 3),
        "fraction_time_z_above_2": _r((z > 2).mean(), 3),
        "p95_z": _r(np.nanpercentile(z, 95), 2),
        "max_z": _r(np.nanmax(z), 2),
    }

    # ---- signal quality / artifact screen ---------------------------------
    gap_thr = max(1.0, 10 * med_dt) if med_dt else 1.0
    gaps = dt[dt > gap_thr]
    gamma = df["Gamma_sm"].to_numpy(dtype=float)
    gstd = np.nanstd(gamma) or 1.0
    gamma_z = (gamma - np.nanmean(gamma)) / gstd
    delta = df["Delta_sm"].to_numpy(dtype=float)
    dstd = np.nanstd(delta) or 1.0
    delta_z = (delta - np.nanmean(delta)) / dstd
    beta_gamma_corr = df["Beta_sm"].corr(df["Gamma_sm"])
    q = dict(quality or {})
    q.update({
        "dropout_gaps": int(len(gaps)),
        "dropout_total_s": _r(gaps.sum(), 1) if len(gaps) else 0.0,
        "gamma_z_above_2_fraction": _r((gamma_z > 2).mean(), 3),
        "delta_z_above_3_fraction": _r((delta_z > 3).mean(), 3),
        "beta_gamma_correlation": _r(beta_gamma_corr, 2),
    })
    flags: list[str] = []
    if dur < 180:
        flags.append(f"Short recording ({dur / 60:.1f} min): per-session statistics are weak.")
    if q.get("headband_off_fraction", 0) and q["headband_off_fraction"] > 0.05:
        flags.append(f"Headband reported off for {q['headband_off_fraction'] * 100:.0f}% of the recording.")
    for ch, v in (q.get("hsi_mean") or {}).items():
        if v >= 2.5:
            flags.append(f"Poor sensor contact on {ch} (mean horseshoe value {v:.1f}; 1 is good, 4 is bad).")
    mins = max(dur / 60.0, 1e-9)
    if q.get("jaw_clenches", 0) and q["jaw_clenches"] / mins > 1:
        flags.append(f"{q['jaw_clenches']} jaw-clench detections ({q['jaw_clenches'] / mins:.1f}/min): muscle artifact likely in beta/gamma.")
    if q.get("blinks", 0) and q["blinks"] / mins > 25:
        flags.append(f"{q['blinks']} blink detections ({q['blinks'] / mins:.0f}/min): frontal delta/theta may be inflated by eye movement.")
    if len(gaps):
        flags.append(f"{len(gaps)} data gap(s) totalling {gaps.sum():.1f}s.")
    if (beta_gamma_corr is not None and np.isfinite(beta_gamma_corr) and beta_gamma_corr > 0.8
            and (gamma_z > 2).mean() > 0.05):
        flags.append("Gamma tracks beta closely with frequent spikes, which is the typical signature of "
                     "muscle (EMG) contamination rather than neural activity.")
    q["flags"] = flags

    # ---- feedback agreement ------------------------------------------------
    feedback = None
    if "Feedback" in df.columns:
        sub = df.dropna(subset=["Feedback"])
        if len(sub) >= 3:
            hit = sub["Feedback"] > 0.5
            feedback = {
                "n_scored": int(len(sub)),
                "n_hit": int(hit.sum()),
                # Spearman = Pearson on ranks (avoids pandas' optional scipy dependency)
                "spearman_rv_z_vs_score": _r(sub["RV_index_z"].rank().corr(sub["Feedback"].rank()), 2),
                "mean_rv_z_when_hit": _r(sub.loc[hit, "RV_index_z"].mean(), 2) if hit.any() else None,
                "mean_rv_z_when_miss": _r(sub.loc[~hit, "RV_index_z"].mean(), 2) if (~hit).any() else None,
                "optimized_threshold": threshold,
            }

    return {
        "session": {
            "name": name, "source_format": source_format,
            "started_at": df["TimeStamp"].iloc[0].isoformat(),
            "duration_min": _r(dur / 60.0, 1), "samples": int(len(df)),
            "sample_interval_s": _r(med_dt, 3), "smoothing_window_samples": params.get("window"),
        },
        "baseline": baseline,
        "timeline": timeline,
        "within_session_trend": trend,
        "events": ev_rows,
        "peak_windows": peaks,
        "peak_context": peak_context,
        "quality": q,
        "feedback": feedback,
    }
