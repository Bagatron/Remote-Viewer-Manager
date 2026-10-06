"""Generate synthetic Mind Monitor-style CSV and the original Excel layout for testing."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

out = Path(sys.argv[1] if len(sys.argv) > 1 else "tests/sample")
out.mkdir(parents=True, exist_ok=True)
rng = np.random.default_rng(42)
n = 3000  # 5 minutes at 10 Hz
ts = pd.date_range("2026-10-05 09:00:00", periods=n, freq="100ms")
t = np.arange(n) / 10


def band(base, amp, phase):
    return base + amp * np.sin(2 * np.pi * t / 60 + phase) + rng.normal(0, 0.15, n)


bands = {"Delta": band(1.0, 0.3, 0), "Theta": band(0.6, 0.4, 1), "Alpha": band(0.5, 0.2, 2),
         "Beta": band(0.3, 0.2, 3.5), "Gamma": band(0.2, 0.05, 0)}

# --- Mind Monitor style CSV: per-channel columns + Elements markers
csv = pd.DataFrame({"TimeStamp": ts.strftime("%Y-%m-%d %H:%M:%S.%f").str[:-3]})
for b, v in bands.items():
    for ch in ["TP9", "AF7", "AF8", "TP10"]:
        csv[f"{b}_{ch}"] = v + rng.normal(0, 0.05, n)
csv["Elements"] = pd.Series([None] * n, dtype=object)
csv.loc[600, "Elements"] = "/Marker/1"
csv.loc[1800, "Elements"] = "/Marker/2"
csv.loc[400, "Elements"] = "/muse/elements/blink"  # housekeeping, should be ignored
csv.to_csv(out / "session_mindmonitor.csv", index=False)

# --- Original Excel layout
x = pd.DataFrame({"TimeStamp": ts, **bands})
fb = pd.DataFrame({"TimeStamp": ts[::150], "Score": (bands["Theta"][::150] > 0.7).astype(float)})
ev = pd.DataFrame({"TimeStamp": [ts[600], ts[1800]], "Label": ["target reveal", "strong impression"]})
with pd.ExcelWriter(out / "session_excel.xlsx") as xw:
    x.to_excel(xw, sheet_name="GraphingDataAve", index=False)
    ev.to_excel(xw, sheet_name="Events", index=False)
    fb.to_excel(xw, sheet_name="Feedback", index=False)
print("wrote", list(out.iterdir()))
