"""
24/7 traffic data collector using HERE APIs.

Collection modes (set in .env -> COLLECT_MODE):
  flow   : HERE Traffic API v7  - speed / jam factor for every road segment in an area
  routes : HERE Routing API v8  - travel time with live traffic for fixed origin->destination pairs
  both   : run both each cycle

Areas and routes are defined in config.json (reloaded every cycle).
Output: one CSV per day per mode in DATA_DIR.
"""
import csv
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ---------- Config (from .env) ----------
API_KEY = os.getenv("HERE_API_KEY", "")
COLLECT_MODE = os.getenv("COLLECT_MODE", "flow").lower()       # flow | routes | both
INTERVAL_SECONDS = int(os.getenv("INTERVAL_SECONDS", "900"))    # 900 = 15 min, 300 = 5 min
CONFIG_FILE = os.getenv("CONFIG_FILE", "/app/config.json")
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
TZ = ZoneInfo(os.getenv("TZ", "Asia/Colombo"))
MAX_RETRIES = 3
REQUEST_TIMEOUT = 30

FLOW_URL = "https://data.traffic.hereapi.com/v7/flow"
ROUTE_URL = "https://router.hereapi.com/v8/routes"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("collector")

FLOW_FIELDS = [
    "timestamp", "source_updated", "area_id", "description", "length_m",
    "start_lat", "start_lng", "end_lat", "end_lng",
    "speed_kmh", "speed_uncapped_kmh", "free_flow_kmh",
    "jam_factor", "confidence", "traversability",
]
ROUTE_FIELDS = [
    "timestamp", "route_id", "origin", "destination",
    "length_m", "duration_s", "base_duration_s", "delay_s", "status",
]

running = True


def stop(signum, frame):
    """Let Docker stop the container cleanly (current cycle finishes first)."""
    global running
    log.info("Stop signal received, shutting down after this cycle...")
    running = False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)


# ---------- helpers ----------
def load_config():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def get_json(url, params):
    """GET with retries. Returns parsed JSON or None.
    Bad request / bad key errors (400/401/403) are NOT retried, so a wrong
    or expired key does not waste quota."""
    params = {**params, "apiKey": API_KEY}
    for attempt in range(1, MAX_RETRIES + 1):
        if not running:
            return None
        try:
            r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if r.status_code == 429:                       # rate limited
                log.warning("Rate limited (429), waiting %ss...", 30 * attempt)
                time.sleep(30 * attempt)
                continue
            if r.status_code in (400, 401, 403):
                log.error("HERE error %s (not retrying): %s", r.status_code, r.text[:300])
                return None
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning("Request failed (attempt %d/%d): %s", attempt, MAX_RETRIES, e)
            time.sleep(5 * attempt)
    return None


def ms_to_kmh(v):
    return round(v * 3.6, 2) if v is not None else None


def save_rows(prefix, fields, rows):
    """Append rows to data/<prefix>_YYYY-MM-DD.csv"""
    if not rows:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    day = rows[0]["timestamp"][:10]
    path = DATA_DIR / f"{prefix}_{day}.csv"
    new_file = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new_file:
            w.writeheader()
        w.writerows(rows)
        f.flush()
        os.fsync(f.fileno())


def heartbeat():
    """Used by the Docker healthcheck."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / ".heartbeat").write_text(str(int(time.time())))


# ---------- HERE Traffic Flow (v7) ----------
def endpoints(location):
    """First and last point of a segment's shape (for map-matching to OSM later)."""
    try:
        links = location["shape"]["links"]
        first = links[0]["points"][0]
        last = links[-1]["points"][-1]
        return first["lat"], first["lng"], last["lat"], last["lng"]
    except (KeyError, IndexError, TypeError):
        return None, None, None, None


def collect_flow(area, ts):
    data = get_json(FLOW_URL, {
        "in": area["in"],                     # "bbox:w,s,e,n" or "circle:lat,lng;r=m"
        "locationReferencing": "shape",
    })
    if data is None:
        log.error("Flow area %s: no data", area["id"])
        return []

    source_updated = data.get("sourceUpdated", "")
    rows = []
    for item in data.get("results", []):
        loc = item.get("location", {})
        flow = item.get("currentFlow", {})
        s_lat, s_lng, e_lat, e_lng = endpoints(loc)
        rows.append({
            "timestamp": ts,
            "source_updated": source_updated,
            "area_id": area["id"],
            "description": loc.get("description", ""),
            "length_m": loc.get("length"),
            "start_lat": s_lat, "start_lng": s_lng,
            "end_lat": e_lat, "end_lng": e_lng,
            "speed_kmh": ms_to_kmh(flow.get("speed")),
            "speed_uncapped_kmh": ms_to_kmh(flow.get("speedUncapped")),
            "free_flow_kmh": ms_to_kmh(flow.get("freeFlow")),
            "jam_factor": flow.get("jamFactor"),
            "confidence": flow.get("confidence"),
            "traversability": flow.get("traversability"),
        })
    log.info("Flow area %s: %d segments", area["id"], len(rows))
    return rows


# ---------- HERE Routing (v8) ----------
def collect_route(route, ts):
    data = get_json(ROUTE_URL, {
        "transportMode": "car",
        "origin": route["origin"],            # "lat,lng"
        "destination": route["destination"],  # "lat,lng"
        "departureTime": datetime.now(TZ).isoformat(timespec="seconds"),
        "return": "summary",
    })
    row = {
        "timestamp": ts, "route_id": route["id"],
        "origin": route["origin"], "destination": route["destination"],
        "length_m": None, "duration_s": None, "base_duration_s": None,
        "delay_s": None, "status": "FAILED",
    }
    try:
        summary = data["routes"][0]["sections"][0]["summary"]
        row.update({
            "length_m": summary.get("length"),
            "duration_s": summary.get("duration"),           # with live traffic
            "base_duration_s": summary.get("baseDuration"),  # without traffic
            "status": "OK",
        })
        if row["duration_s"] is not None and row["base_duration_s"] is not None:
            row["delay_s"] = row["duration_s"] - row["base_duration_s"]
    except (TypeError, KeyError, IndexError):
        log.warning("Route %s: no route returned", route["id"])
    return row


# ---------- main loop ----------
def run_cycle(cfg):
    ts = datetime.now(TZ).isoformat(timespec="seconds")

    if COLLECT_MODE in ("flow", "both"):
        rows = []
        for area in cfg.get("areas", []):
            if not running:
                break
            rows.extend(collect_flow(area, ts))
        save_rows("flow", FLOW_FIELDS, rows)

    if COLLECT_MODE in ("routes", "both"):
        rows = []
        for route in cfg.get("routes", []):
            if not running:
                break
            rows.append(collect_route(route, ts))
        save_rows("routes", ROUTE_FIELDS, rows)
        ok = sum(r["status"] == "OK" for r in rows)
        log.info("Routes: %d/%d OK", ok, len(rows))


def main():
    if not API_KEY:
        log.error("HERE_API_KEY is not set. Check your .env file.")
        sys.exit(1)
    if COLLECT_MODE not in ("flow", "routes", "both"):
        log.error("COLLECT_MODE must be flow, routes or both (got %s)", COLLECT_MODE)
        sys.exit(1)

    log.info("Collector started. mode=%s interval=%ss data=%s",
             COLLECT_MODE, INTERVAL_SECONDS, DATA_DIR)

    while running:
        start = time.time()
        try:
            run_cycle(load_config())   # config reloaded each cycle
            heartbeat()
        except Exception as e:
            log.exception("Cycle failed: %s", e)

        # sleep in small steps so stop signals are handled quickly
        wait = max(0, INTERVAL_SECONDS - (time.time() - start))
        while running and wait > 0:
            time.sleep(min(5, wait))
            wait -= 5

    log.info("Collector stopped.")


if __name__ == "__main__":
    main()
