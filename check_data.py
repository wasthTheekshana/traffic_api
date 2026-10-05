"""
Checks whether the collected HERE route data contains real traffic.
Run on the server:  docker compose exec collector python check_data.py
"""
import csv
import glob
import os
from collections import defaultdict

DATA_DIR = os.getenv("DATA_DIR", "/app/data")
files = sorted(glob.glob(os.path.join(DATA_DIR, "routes_*.csv")))
if not files:
    print("No routes_*.csv files yet in", DATA_DIR)
    raise SystemExit

rows = []
for f in files:
    with open(f, encoding="utf-8") as fh:
        rows.extend(r for r in csv.DictReader(fh) if r["status"] == "OK")

if not rows:
    print("Files found but no successful rows yet.")
    raise SystemExit

times = sorted({r["timestamp"] for r in rows})
print(f"Files       : {len(files)}  ({os.path.basename(files[0])} .. {os.path.basename(files[-1])})")
print(f"Samples     : {len(times)}  ({times[0]}  ->  {times[-1]})")
print(f"OK rows     : {len(rows)}")

delayed = [r for r in rows if float(r["delay_s"] or 0) != 0]
print(f"Delay > 0   : {len(delayed)} rows ({100 * len(delayed) / len(rows):.1f}%)")
print()

by_seg = defaultdict(list)
for r in rows:
    by_seg[r["route_id"]].append(int(float(r["duration_s"])))

print(f"{'Segment':26} {'samples':>7} {'unique':>6} {'min_s':>6} {'max_s':>6} {'max_delay':>9}")
for seg, durs in sorted(by_seg.items()):
    seg_rows = [r for r in rows if r["route_id"] == seg]
    max_delay = max(float(r["delay_s"] or 0) for r in seg_rows)
    print(f"{seg:26} {len(durs):>7} {len(set(durs)):>6} {min(durs):>6} {max(durs):>6} {max_delay:>9.0f}")

print()
changing = sum(1 for d in by_seg.values() if len(set(d)) > 1)
if delayed or changing:
    print(f"RESULT: TRAFFIC FOUND - {changing}/{len(by_seg)} segments change over time. HERE data is usable.")
else:
    print("RESULT: NO TRAFFIC - every segment has one constant duration (free-flow only).")
    print("        HERE has no traffic data for these roads. Do not use this for training.")
