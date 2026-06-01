# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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
pi/          Part 1 — Python OBD collector (Raspberry Pi 3B) ← COMPLETE
backend/     Part 2 — Golang API + PostgreSQL + Grafana      ← NOT STARTED
frontend/    Part 3 — Web dashboard (deferred)
```

Parts are built sequentially. Part 1 must be fully working before Part 2 starts.

## Commands (pi/)

**Python**: Use `/usr/bin/python3` on this Mac — Homebrew Python 3.14 is broken. Venv is at `pi/venv/`.

```bash
# Install dev dependencies (first time or after pulling)
cd pi && /usr/bin/python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# Run all tests with coverage
cd pi && /usr/bin/python3 -m pytest tests/ --cov=src

# Run a single test file
cd pi && /usr/bin/python3 -m pytest tests/test_collector.py -v

# Run a single test by name
cd pi && /usr/bin/python3 -m pytest tests/test_trip.py::test_trip_start_requires_voltage_and_rpm -v
```

Tests run without hardware. `conftest.py` sets required env vars before any `src/` import and inserts `src/` into `sys.path` — no install step needed.

## Part 1 Status (Pi collector)
All 16 coding tasks complete. Full code review done — all critical bugs fixed. 199 unit tests, 97% coverage. Ready for hardware deployment.

Remaining before deployment:
- FORScan baseline scan (needs OBDLink MX+ + car) — confirms Mode 22 hex addresses
- Ford PID entries in `obd_commands.py` (after FORScan)
- Pi OS setup, DS3231 wiring, USB drive format (ext4), BT pairing
- `install.sh` run + `config.env` filled in

## Conventions
- No Claude/Anthropic attribution anywhere — no co-author trailers, no PR descriptions, nothing GitHub-visible
- Commit messages are plain, no co-author lines
- No comments unless the WHY is non-obvious
- Units always in column/variable names: `coolant_temp_c`, `battery_v`, `speed_kmh`
- UUID primary keys on all database tables
- ISO8601 timestamps everywhere
- `from __future__ import annotations` in all Pi source files (Python 3.9 compat)

## Key Decisions
- Polling tiers: 1s / 5s / 30s (standard OBD) + ford_obd_5s / 10s / 20s (Mode 22)
- Single `obd.Async` connection with time-filter callbacks enforcing `interval_s` per PID — python-obd 0.7.3 fires all watchers at ~1Hz regardless of registration interval
- Trip start: voltage >13V AND RPM>0 — Trip end: RPM=0 for >30s AND voltage <12.5V
- Polling pause: obd_1s + obd_5s suppressed when RPM=0 for >30s; obd_30s keeps running
- NULL stored for bad PID responses — never carry forward last known value
- SQLite mirrors PostgreSQL structure exactly — same tables, same columns, same units
- UUID per row for sync deduplication — `ON CONFLICT (id) DO NOTHING` on all inserts
- Sync endpoint only over Tailscale + API key header (both required)
- WAL mode on ext4 USB drive — abrupt power cuts are safe, SQLite replays WAL on next open
- All SQLite writes serialised through `QueueWriter`; thread safety via `_db_lock`
- `reconnect_count` and `restart_count` persisted to USB drive files so sync script can read them independently of the collector process

## Data Volume
~192,000 rows/month based on 1hr/day Mon–Sat + 4hrs Sunday.
System runs until 300,000 km — not a fixed time horizon.
