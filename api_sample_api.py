"""
Colombo traffic collector - Google Routes API (traffic-aware), free-tier safe.

Every INTERVAL_SECONDS (aligned to local clock, e.g. 06:00, 07:00 ...) and only
inside ACTIVE_HOURS, it requests each route in config.json and saves:
  duration_s         travel time WITH live traffic
  static_duration_s  travel time WITHOUT traffic
  delay_s            duration - static  (the traffic signal)

Cost safety:
  - DAILY_LIMIT / MONTHLY_LIMIT: hard caps counted in data/usage.json.
    When a cap is reached, no more requests are sent until the next day/month.
  - 400/403/429 are never retried (no wasted or billed calls).
  - Also set a daily quota in Google Cloud Console as a second cap.
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

API_KEY = os.getenv("GOOGLE_API_KEY", "")
INTERVAL_SECONDS = int(os.getenv("INTERVAL_SECONDS", "3600"))
ACTIVE_HOURS = os.getenv("ACTIVE_HOURS", "6-21")          # inclusive local hours
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "160"))
MONTHLY_LIMIT = int(os.getenv("MONTHLY_LIMIT", "4900"))
CONFIG_FILE = os.getenv("CONFIG_FILE", "/app/config.json")
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
TZ = ZoneInfo(os.getenv("TZ", "Asia/Colombo"))
REQUEST_TIMEOUT = 30
MAX_RETRIES = 2

URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
FIELD_MASK = "routes.duration,routes.staticDuration,routes.distanceMeters"
USAGE_FILE = DATA_DIR / "usage.json"

FIELDS = [
    "timestamp", "route_id", "origin", "destination", "length_m",
    "duration_s", "static_duration_s", "delay_s", "speed_kmh", "status",
]

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("collector")

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


# ---------- Google request ----------
def latlng(s):
    lat, lng = (float(x) for x in s.split(","))
    return {"location": {"latLng": {"latitude": lat, "longitude": lng}}}


def secs(v):
    """Google returns durations like '520s'."""
    return int(float(v.rstrip("s"))) if v else None


def compute_route(route, usage):
    body = {
        "origin": latlng(route["origin"]),
        "destination": latlng(route["destination"]),
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",   # Pro SKU. Never use TRAFFIC_AWARE_OPTIMAL (Enterprise)
    }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    for attempt in range(1, MAX_RETRIES + 1):
        if not running:
            return None, "STOPPED"
        if not can_send(usage):
            return None, "LIMIT"
        try:
            count_request(usage)                 # count before sending = conservative
            r = requests.post(URL, json=body, headers=headers, timeout=REQUEST_TIMEOUT)
        except Exception as e:
            log.warning("%s: network error (attempt %d): %s", route["id"], attempt, e)
            time.sleep(5 * attempt)
            continue
        if r.status_code == 200:
            return r.json(), "OK"
        if r.status_code in (400, 401, 403, 429):
            log.error("%s: Google error %s (not retrying): %s",
                      route["id"], r.status_code, r.text[:300])
            return None, f"HTTP_{r.status_code}"
        log.warning("%s: Google error %s (attempt %d)", route["id"], r.status_code, attempt)
        time.sleep(5 * attempt)
    return None, "FAILED"


def collect_route(route, ts, usage):
    row = {"timestamp": ts, "route_id": route["id"], "origin": route["origin"],
           "destination": route["destination"], "length_m": None, "duration_s": None,
           "static_duration_s": None, "delay_s": None, "speed_kmh": None}
    data, status = compute_route(route, usage)
    row["status"] = status
    if data:
        try:
            r0 = data["routes"][0]
            row["length_m"] = r0.get("distanceMeters")
            row["duration_s"] = secs(r0.get("duration"))
            row["static_duration_s"] = secs(r0.get("staticDuration"))
            if row["duration_s"] is not None and row["static_duration_s"] is not None:
                row["delay_s"] = row["duration_s"] - row["static_duration_s"]
            if row["duration_s"] and row["length_m"]:
                row["speed_kmh"] = round(row["length_m"] / row["duration_s"] * 3.6, 2)
        except (KeyError, IndexError, TypeError):
            row["status"] = "NO_ROUTE"
    return row


# ---------- saving ----------
def save_rows(rows):
    if not rows:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"google_routes_{rows[0]['timestamp'][:10]}.csv"
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
    """Next slot aligned to local midnight + k*INTERVAL (e.g. 06:00, 07:00)."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    k = int((now - midnight).total_seconds() // INTERVAL_SECONDS) + 1
    return midnight + timedelta(seconds=k * INTERVAL_SECONDS)


def run_cycle():
    now = datetime.now(TZ)
    ts = now.isoformat(timespec="seconds")
    routes = json.loads(Path(CONFIG_FILE).read_text())["routes"]
    usage = load_usage(now)
    rows = []
    for route in routes:
        if not running:
            break
        rows.append(collect_route(route, ts, usage))
        if rows[-1]["status"] == "LIMIT":
            log.warning("Usage limit reached (day %d/%d, month %d/%d) - skipping rest",
                        usage["day_count"], DAILY_LIMIT, usage["month_count"], MONTHLY_LIMIT)
            rows.pop()
            break
        time.sleep(0.2)
    save_rows(rows)
    ok = sum(r["status"] == "OK" for r in rows)
    delayed = sum(1 for r in rows if r["delay_s"])
    log.info("Routes: %d/%d OK, %d with traffic delay > 0 | used today %d/%d, month %d/%d",
             ok, len(routes), delayed, usage["day_count"], DAILY_LIMIT,
             usage["month_count"], MONTHLY_LIMIT)


def main():
    if not API_KEY:
        log.error("GOOGLE_API_KEY is not set. Check your .env file.")
        sys.exit(1)
    n = len(json.loads(Path(CONFIG_FILE).read_text())["routes"])
    start, end = (int(x) for x in ACTIVE_HOURS.split("-"))
    runs = sum(1 for h in range(24) for m in range(0, 3600, INTERVAL_SECONDS)
               if start <= h <= end) if INTERVAL_SECONDS <= 3600 else (end - start + 1)
    log.info("Collector started. routes=%d interval=%ss hours=%s -> ~%d requests/day "
             "(limits: %d/day, %d/month)", n, INTERVAL_SECONDS, ACTIVE_HOURS,
             n * runs, DAILY_LIMIT, MONTHLY_LIMIT)
    if n * runs > DAILY_LIMIT:
        log.warning("Planned requests/day (%d) > DAILY_LIMIT (%d): later runs each day will be skipped.",
                    n * runs, DAILY_LIMIT)
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
        heartbeat()   # also beats outside active hours, so healthcheck stays green

    log.info("Collector stopped.")


if __name__ == "__main__":
    main()
