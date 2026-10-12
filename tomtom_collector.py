"""
Colombo traffic collector - TomTom Traffic Flow Segment Data API, free-tier safe.

Every INTERVAL_SECONDS (aligned to local clock, e.g. 00:00, 00:30, 01:00 ...) and
inside ACTIVE_HOURS (default: all day), it requests each point in POINTS_FILE.
TomTom snaps the point to the nearest road segment and returns:
  current_speed_kmh     live speed on that segment
  free_flow_speed_kmh   speed with no traffic
  speed_ratio           current / free_flow   (1.0 = free flow, 0.5 = half speed)
  confidence            0..1, how much live probe data backs the value

Cost safety (same approach as the Google collector):
  - DAILY_LIMIT / MONTHLY_LIMIT: hard caps counted in DATA_DIR/tomtom_usage.json.
  - 400/403/429 are never retried.
  - Free tier: 20,000 Flow Segment Data requests/month.
    10 points x 48 runs/day x 31 days = 14,880 -> inside the free tier.

Usage:
  python tomtom_collector.py           # run forever (Docker)
  python tomtom_collector.py --check   # query every point once, print what TomTom matched
"""
import csv
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

API_KEY = os.getenv("TOMTOM_API_KEY", "")
INTERVAL_SECONDS = int(os.getenv("INTERVAL_SECONDS", "1800"))
ACTIVE_HOURS = os.getenv("ACTIVE_HOURS", "0-23")          # inclusive local hours
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "640"))
MONTHLY_LIMIT = int(os.getenv("MONTHLY_LIMIT", "19000"))
POINTS_FILE = os.getenv("POINTS_FILE", "/app/tomtom_points.json")
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data/tomtom"))
TZ = ZoneInfo(os.getenv("TZ", "Asia/Colombo"))
ZOOM = int(os.getenv("TOMTOM_ZOOM", "10"))
REQUEST_TIMEOUT = 30
MAX_RETRIES = 2
SLEEP_BETWEEN = 1.0                                        # seconds between requests

URL = f"https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/{ZOOM}/json"
USAGE_FILE = DATA_DIR / "tomtom_usage.json"

FIELDS = [
    "timestamp", "point_id", "lat", "lon", "frc",
    "current_speed_kmh", "free_flow_speed_kmh", "speed_ratio",
    "current_tt_s", "free_flow_tt_s", "confidence", "road_closure",
    "seg_start", "seg_end", "seg_points", "status",
]

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tomtom")

running = True


def stop(signum, frame):
    global running
    log.info("Stop signal received, shutting down...")
    running = False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)


# ---------- usage counter (hard cost cap) ----------
def load_usage(now):
    try:
        u = json.loads(USAGE_FILE.read_text())
    except Exception:
        u = {}
    day, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
    if u.get("month") != month:
        u["month"], u["month_count"] = month, 0
    if u.get("day") != day:
        u["day"], u["day_count"] = day, 0
    return u


def save_usage(u):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = USAGE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(u))
    tmp.replace(USAGE_FILE)


def can_send(u):
    return u["day_count"] < DAILY_LIMIT and u["month_count"] < MONTHLY_LIMIT


def count_request(u):
    u["day_count"] += 1
    u["month_count"] += 1
    save_usage(u)


# ---------- TomTom request ----------
def flow_segment(point, usage):
    params = {"key": API_KEY, "point": f"{point['lat']},{point['lon']}", "unit": "kmph"}
    for attempt in range(1, MAX_RETRIES + 1):
        if not running:
            return None, "STOPPED"
        if not can_send(usage):
            return None, "LIMIT"
        try:
            count_request(usage)                 # count before sending = conservative
            r = requests.get(URL, params=params, timeout=REQUEST_TIMEOUT)
        except Exception as e:
            # Never log the request URL: it contains the API key
            log.warning("%s: network error (attempt %d): %s",
                        point["id"], attempt, type(e).__name__)
            time.sleep(5 * attempt)
            continue
        if r.status_code == 200:
            return r.json(), "OK"
        if r.status_code in (400, 401, 403, 429):
            log.error("%s: TomTom error %s (not retrying): %s",
                      point["id"], r.status_code, r.text[:300])
            return None, f"HTTP_{r.status_code}"
        log.warning("%s: TomTom error %s (attempt %d)", point["id"], r.status_code, attempt)
        time.sleep(5 * attempt)
    return None, "FAILED"


def collect_point(point, ts, usage):
    row = {k: None for k in FIELDS}
    row.update(timestamp=ts, point_id=point["id"], lat=point["lat"], lon=point["lon"])
    data, status = flow_segment(point, usage)
    row["status"] = status
    if data:
        try:
            f = data["flowSegmentData"]
            row["frc"] = f.get("frc")
            row["current_speed_kmh"] = f.get("currentSpeed")
            row["free_flow_speed_kmh"] = f.get("freeFlowSpeed")
            row["current_tt_s"] = f.get("currentTravelTime")
            row["free_flow_tt_s"] = f.get("freeFlowTravelTime")
            row["confidence"] = f.get("confidence")
            row["road_closure"] = f.get("roadClosure")
            if row["current_speed_kmh"] and row["free_flow_speed_kmh"]:
                row["speed_ratio"] = round(row["current_speed_kmh"] / row["free_flow_speed_kmh"], 3)
            coords = f.get("coordinates", {}).get("coordinate", [])
            if coords:
                row["seg_start"] = f"{coords[0]['latitude']:.5f},{coords[0]['longitude']:.5f}"
                row["seg_end"] = f"{coords[-1]['latitude']:.5f},{coords[-1]['longitude']:.5f}"
                row["seg_points"] = len(coords)
        except (KeyError, IndexError, TypeError):
            row["status"] = "NO_SEGMENT"
    return row


# ---------- saving ----------
def save_rows(rows):
    if not rows:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"tomtom_flow_{rows[0]['timestamp'][:10]}.csv"
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)
        f.flush()
        os.fsync(f.fileno())


def heartbeat():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / ".heartbeat").write_text(str(int(time.time())))


# ---------- schedule ----------
def active(now):
    start, end = (int(x) for x in ACTIVE_HOURS.split("-"))
    return start <= now.hour <= end


def next_run(now):
    """Next slot aligned to local midnight + k*INTERVAL (e.g. 00:00, 00:30, 01:00)."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    k = int((now - midnight).total_seconds() // INTERVAL_SECONDS) + 1
    return midnight + timedelta(seconds=k * INTERVAL_SECONDS)


def load_points():
    return json.loads(Path(POINTS_FILE).read_text())["points"]


def run_cycle():
    now = datetime.now(TZ)
    # Timestamp of the slot, not of the moment the request finished
    ts = now.replace(second=0, microsecond=0).isoformat(timespec="seconds")
    points = load_points()
    usage = load_usage(now)
    rows = []
    for point in points:
        if not running:
            break
        rows.append(collect_point(point, ts, usage))
        if rows[-1]["status"] == "LIMIT":
            log.warning("Usage limit reached (day %d/%d, month %d/%d) - skipping rest",
                        usage["day_count"], DAILY_LIMIT, usage["month_count"], MONTHLY_LIMIT)
            rows.pop()
            break
        time.sleep(SLEEP_BETWEEN)
    save_rows(rows)
    ok = [r for r in rows if r["status"] == "OK"]
    slow = sum(1 for r in ok if r["speed_ratio"] is not None and r["speed_ratio"] < 0.8)
    log.info("Points: %d/%d OK, %d below 80%% of free-flow | used today %d/%d, month %d/%d",
             len(ok), len(points), slow, usage["day_count"], DAILY_LIMIT,
             usage["month_count"], MONTHLY_LIMIT)


def check():
    """One request per point; shows which road segment TomTom matched."""
    usage = load_usage(datetime.now(TZ))
    print(f"{'point_id':<26}{'frc':<6}{'now':>6}{'free':>6}{'ratio':>7}{'conf':>6}  segment")
    for p in load_points():
        r = collect_point(p, datetime.now(TZ).isoformat(timespec="seconds"), usage)
        if r["status"] != "OK":
            print(f"{p['id']:<26}{r['status']}")
        else:
            print(f"{p['id']:<26}{r['frc']:<6}{r['current_speed_kmh']:>6}"
                  f"{r['free_flow_speed_kmh']:>6}{r['speed_ratio']:>7}{r['confidence']:>6}"
                  f"  {r['seg_start']} -> {r['seg_end']}")
        time.sleep(SLEEP_BETWEEN)
    print(f"\nUsed today {usage['day_count']}/{DAILY_LIMIT}, month {usage['month_count']}/{MONTHLY_LIMIT}")
    print("frc: FRC0 motorway, FRC1 major road, FRC2 other major, FRC3 secondary, "
          "FRC4 local connecting, FRC5-6 minor")


def main():
    if not API_KEY:
        log.error("TOMTOM_API_KEY is not set. Check your .env file.")
        sys.exit(1)
    if "--check" in sys.argv:
        check()
        return
    n = len(load_points())
    start, end = (int(x) for x in ACTIVE_HOURS.split("-"))
    runs_per_day = (end - start + 1) * 3600 // INTERVAL_SECONDS
    log.info("TomTom collector started. points=%d interval=%ss hours=%s -> ~%d requests/day, "
             "~%d/month (limits: %d/day, %d/month)", n, INTERVAL_SECONDS, ACTIVE_HOURS,
             n * runs_per_day, n * runs_per_day * 31, DAILY_LIMIT, MONTHLY_LIMIT)
    if n * runs_per_day > DAILY_LIMIT:
        log.warning("Planned requests/day (%d) > DAILY_LIMIT (%d): later runs each day will be skipped.",
                    n * runs_per_day, DAILY_LIMIT)
    heartbeat()

    while running:
        nxt = next_run(datetime.now(TZ))
        log.info("Next run at %s", nxt.strftime("%Y-%m-%d %H:%M"))
        while running and datetime.now(TZ) < nxt:
            time.sleep(min(10, max(0.5, (nxt - datetime.now(TZ)).total_seconds())))
        if not running:
            break
        if active(datetime.now(TZ)):
            try:
                run_cycle()
            except Exception as e:
                log.exception("Cycle failed: %s", e)
        heartbeat()

    log.info("TomTom collector stopped.")


if __name__ == "__main__":
    main()