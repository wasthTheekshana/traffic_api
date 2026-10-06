"""
One-off test: 4 corridors, 4 requests (counted in usage.json).
Run:  docker compose run --rm collector python test_google.py
"""
import api_sample_api as c
from datetime import datetime

TEST = [
    {"id": "Fort -> Borella",       "origin": "6.9340,79.8455", "destination": "6.9105,79.8702"},
    {"id": "Nugegoda -> Borella",   "origin": "6.8697,79.8995", "destination": "6.9105,79.8702"},
    {"id": "Dehiwala -> Colombo 3", "origin": "6.8519,79.8617", "destination": "6.9003,79.8540"},
    {"id": "Peliyagoda -> Pettah",  "origin": "6.9700,79.8928", "destination": "6.9357,79.8534"},
]
now = datetime.now(c.TZ)
usage = c.load_usage(now)
print(f"Test at {now:%Y-%m-%d %H:%M}  (HERE gave delay 0 on all of these)\n")
print(f"{'Corridor':24} {'traffic_s':>9} {'static_s':>8} {'delay_s':>7} {'km/h':>6}  status")
found = 0
for r in TEST:
    row = c.collect_route(r, now.isoformat(), usage)
    print(f"{r['id']:24} {str(row['duration_s']):>9} {str(row['static_duration_s']):>8} "
          f"{str(row['delay_s']):>7} {str(row['speed_kmh']):>6}  {row['status']}")
    if row["delay_s"]:
        found += 1
print(f"\nUsage: today {usage['day_count']}, month {usage['month_count']}")
if found:
    print(f"RESULT: Google HAS live traffic for Colombo ({found}/4 corridors delayed). Start the collector.")
else:
    print("RESULT: no delay right now. Retest in peak hours (8-9 AM / 5-7 PM) before deciding.")
