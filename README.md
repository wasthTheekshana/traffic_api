# traffic_api - Colombo traffic collector (Google Routes API)

Hourly traffic-aware travel times for 10 Colombo corridors (06:00-21:00), saved to
`/mnt/disks/data/traffic/google_routes_YYYY-MM-DD.csv`. Designed to stay inside
Google's free 5,000 requests/month. Full setup: **GOOGLE_SETUP.txt**.

Columns: timestamp, route_id, origin, destination, length_m, duration_s (with traffic),
static_duration_s (no traffic), delay_s, speed_kmh, status

- `test_google.py` - one-off 4-request check that Google has Colombo traffic
- `check_data.py`  - summary, usage counter, average delay by hour
- `routes_spare.json` - 30 more corridors for later
- Hard caps in code (DAILY_LIMIT / MONTHLY_LIMIT) + Google daily quota + budget alert
