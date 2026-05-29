# Project 300K — CLAUDE.md

## Goal
Make a 2023 Ford Bronco Sport Badlands comfortably run beyond 300,000 km.
Active longevity engineering — catch problems early, drive maintenance decisions, build a complete health record from day one.

## Architecture
```
OBDLink MX+ → Bluetooth → Raspberry Pi 3B (in car)
    └── SQLite (USB flash drive, WAL mode, permanent storage)
        └── Sync via iPhone hotspot (every 5 min after boot)
            └── Golang API (home server, Tailscale only)
                └── PostgreSQL
                    ├── Grafana dashboards + alerts
                    └── Claude API analysis (on demand + weekly + DTC triggered)
```

## Repo Layout
```
pi/          Part 1 — Python OBD collector (Raspberry Pi 3B)
backend/     Part 2 — Golang API + PostgreSQL + Grafana
frontend/    Part 3 — Web dashboard (deferred)
```

## Build Order
Parts are built sequentially. Part 1 must be fully working before Part 2 starts.
Part 3 is deferred until Part 2 is stable.

## Conventions
- No Claude/Anthropic attribution anywhere — no co-author trailers, no PR descriptions, nothing GitHub-visible
- Commit messages are plain, no co-author lines
- No comments unless the WHY is non-obvious
- Units always in column/variable names: coolant_temp_c, battery_v, speed_kmh
- UUID primary keys on all database tables
- ISO8601 timestamps everywhere

## Key Decisions (summary — full detail in memory files)
- Polling tiers: 1s / 5s / 30s (standard OBD) + ford_obd_5s / 10s / 20s (Mode 22)
- Trip start: voltage >13V AND RPM>0 — Trip end: RPM=0 for >30s AND voltage <12.5V
- Polling pause: obd_1s + obd_5s pause when RPM=0 for >30s; obd_30s keeps running
- NULL stored for bad PID responses — never carry forward last known value
- SQLite mirrors PostgreSQL structure exactly — same tables, same columns, same units
- UUID per row for sync deduplication — ON CONFLICT (id) DO NOTHING on all inserts
- Sync endpoint only over Tailscale + API key header (both required)
- PostgreSQL backup: nightly pg_dump → rclone → Google Drive

## Data Volume
~192,000 rows/month based on 1hr/day Mon–Sat + 4hrs Sunday.
System runs until 300,000 km — not a fixed time horizon.
