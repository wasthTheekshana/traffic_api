"""
Summary of collected Google data.
Run:  docker compose exec collector python check_data.py
"""
import csv, glob, json, os
from collections import defaultdict

DATA_DIR = os.getenv("DATA_DIR", "/app/data")
files = sorted(glob.glob(os.path.join(DATA_DIR, "google_routes_*.csv")))
try:
    u = json.load(open(os.path.join(DATA_DIR, "usage.json")))
    print(f"Usage       : today {u['day_count']} | month {u['month']} {u['month_count']}  (free: 5,000/month)")
except Exception:
    pass
if not files:
    print("No google_routes_*.csv files yet.")
    raise SystemExit

rows = []
for f in files:
    with open(f, encoding="utf-8") as fh:
        rows.extend(csv.DictReader(fh))
ok = [r for r in rows if r["status"] == "OK"]
bad = len(rows) - len(ok)
times = sorted({r["timestamp"] for r in rows})
print(f"Files       : {len(files)}  ({os.path.basename(files[0])} .. {os.path.basename(files[-1])})")
print(f"Samples     : {len(times)}  ({times[0]}  ->  {times[-1]})")
print(f"Rows        : {len(ok)} OK, {bad} failed")
if not ok:
    raise SystemExit
delayed = [r for r in ok if float(r["delay_s"] or 0) > 0]
print(f"Delay > 0   : {len(delayed)} rows ({100 * len(delayed) / len(ok):.1f}%)\n")

seg = defaultdict(list)
for r in ok:
    seg[r["route_id"]].append(r)
print(f"{'Segment':26} {'n':>4} {'uniq':>4} {'min_s':>6} {'max_s':>6} {'avg_delay':>9} {'max_delay':>9}")
for s, rs in sorted(seg.items()):
    d = [int(r["duration_s"]) for r in rs]
    dl = [float(r["delay_s"] or 0) for r in rs]
    print(f"{s:26} {len(rs):>4} {len(set(d)):>4} {min(d):>6} {max(d):>6} {sum(dl)/len(dl):>9.0f} {max(dl):>9.0f}")

hours = defaultdict(list)
for r in ok:
    hours[r["timestamp"][11:13]].append(float(r["delay_s"] or 0))
print("\nAverage delay by hour (all corridors):")
for h in sorted(hours):
    v = sum(hours[h]) / len(hours[h])
    print(f"  {h}:00  {v:6.0f} s  {'#' * int(v // 10)}")

changing = sum(1 for rs in seg.values() if len({r['duration_s'] for r in rs}) > 1)
print()
if delayed or changing:
    print(f"RESULT: TRAFFIC FOUND - {changing}/{len(seg)} segments change over time.")
else:
    print("RESULT: NO TRAFFIC so far.")
