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

See `pi/CLAUDE.md` for detailed Pi internals: threading model, OBD connection rules, SQLite thread-safety contract, polling tier PID table, shutdown order, Mode 22 hex addresses, and systemd service specs.

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
- No polling pause — all tiers record continuously during an active trip (idle/ESS-stop data is kept; filter on `rpm > 0` in Grafana when an average should exclude idle)
- NULL stored for bad PID responses — never carry forward last known value
- SQLite mirrors PostgreSQL structure exactly — same tables, same columns, same units
- UUID per row for sync deduplication — `ON CONFLICT (id) DO NOTHING` on all inserts
- Sync endpoint only over Tailscale + API key header (both required)
- WAL mode on ext4 USB drive — abrupt power cuts are safe, SQLite replays WAL on next open
- All SQLite writes serialised through `QueueWriter`; thread safety via `_db_lock`
- `reconnect_count` and `restart_count` persisted to USB drive files so sync script can read them independently of the collector process

## Critical Non-Obvious Rules (Pi)

**OBD connection must use `fast=False`**: `obd.OBD("/dev/rfcomm0", fast=False, timeout=30)` — omitting it drops the connection on the Pi Bluetooth stack. `obd.Async` must be instantiated with a port string directly, not wrapped around an existing `obd.OBD` object.

**All SQLite writes must go through `QueueWriter`**: Use `enqueue()` for INSERT paths and `direct_execute()` for UPDATE paths. Never call `conn.execute()` directly from outside `QueueWriter` — `_db_lock` will not protect it.

**DTC scans must stop the async loop first**: `collector.query_sync()` stops `obd.Async`, issues the synchronous `GET_DTC` query, then restarts the loop. Without this, the async polling thread consumes the DTC response bytes on `/dev/rfcomm0` and the synchronous `query()` call times out. A `_dtc_lock` serialises concurrent DTC scans (e.g. trip-start and trip-end overlapping).

**Collector uses combined rows per table, not one row per PID**: Each PID callback accumulates its value into a per-table buffer (`_table_buffer`). When `interval_s` elapses for that table, the entire buffer is flushed as one combined row via `QueueWriter.enqueue()`. This means `obd_1s` gets one row/second with `rpm`, `speed_kmh`, `throttle_pct`, and `load_pct` all in a single row — not four separate sparse rows. The schema is designed around this.

**Shutdown order is fixed** — DTC threads must finish before disconnect; queue must drain before `conn.close()`:
```
collector.stop() → trip_manager.stop() → queue_writer.stop() → obd_connection.disconnect() → conn.close()
```

**systemd services** (4 total):
- `obd-collector.service` — Type=notify, WatchdogSec=60, Restart=always
- `obd-sync.service` — oneshot, runs the sync script
- `obd-sync.timer` — fires `obd-sync.service` every 5 min after boot
- `rfcomm-connect.service` — binds `/dev/rfcomm0` to the OBDLink MX+ MAC address at boot; uses `Wants=` (not `Requires=`) so `obd-collector.service` starts even if BT binding fails on the first attempt; includes `ExecStartPre=-/usr/bin/rfcomm release 0` to clear any stale binding from a previous session before connecting

**Schema is create-only**: `storage.init_schema()` runs `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS` on every boot — no migration logic. The schema is treated as final; changing a column on an already-deployed DB requires wiping `obd.db` (the data is synced to PostgreSQL, the USB copy is a backup).

## Data Volume
~192,000 rows/month based on 1hr/day Mon–Sat + 4hrs Sunday.
System runs until 300,000 km — not a fixed time horizon.
