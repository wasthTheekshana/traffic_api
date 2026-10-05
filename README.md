# api_sample_api - 24/7 HERE traffic collector

Collects HERE traffic data on a fixed interval and saves daily CSV files to
`/mnt/disks/data/traffic` on the server. Full step-by-step setup: **SETUP.txt**.

## Modes (`COLLECT_MODE` in `.env`)
- `flow` - HERE Traffic API v7. Speed, free-flow speed and jam factor (0-10) for every
  road segment inside each area in `config.json`, with segment start/end coordinates
  for map-matching to OSM. Output: `flow_YYYY-MM-DD.csv`
- `routes` - HERE Routing API v8. Travel time with vs. without traffic for each
  origin -> destination pair. Output: `routes_YYYY-MM-DD.csv`
- `both` - runs both each cycle.

## Output columns
`flow`: timestamp (Asia/Colombo), source_updated (HERE data time, UTC), area_id,
description, length_m, start/end lat/lng, speed_kmh, speed_uncapped_kmh,
free_flow_kmh, jam_factor, confidence, traversability

`routes`: timestamp, route_id, origin, destination, length_m, duration_s,
base_duration_s, delay_s, status

## Notes
- Runs as non-root (UID 1000), no open ports, no-new-privileges, 0.5 CPU / 256 MB limit.
- `stop_grace_period: 90s` lets the current cycle finish before Docker stops it.
- 400/401/403 errors are not retried, so a bad key does not waste quota.
- `config.json` is reloaded every cycle - no restart needed after edits.
