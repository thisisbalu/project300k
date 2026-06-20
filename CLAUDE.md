# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Goal
Make a 2023 Ford Bronco Sport Badlands comfortably run beyond 300,000 km.
Active longevity engineering — catch problems early, drive maintenance decisions, build a complete health record from day one.

## Architecture
```
OBDLink MX+ → Bluetooth → Raspberry Pi 3B (in car)
    └── SQLite (USB flash drive, WAL mode, permanent storage)
        └── Sync via iPhone hotspot (once per drive, boot-triggered, over Tailscale)
            └── Golang API (home server, Tailscale only)
                └── PostgreSQL
                    ├── Grafana dashboards + alerts
                    └── Claude API analysis (on demand + weekly + DTC triggered)
```

**Home server is temporarily a laptop** (the real mini-PC isn't bought yet — ~2 months).
The backend runs as a Docker stack (Tailscale sidecar + PostgreSQL + Go API) on the laptop;
see `backend/README.md`. The Pi reaches it over the private tailnet only.

## Repo Layout
```
pi/          Part 1 — Python OBD collector (Raspberry Pi 3B) ← COMPLETE, live in car
backend/     Part 2 — Golang API + PostgreSQL + Grafana      ← BUILT (API, Grafana, backups, alerts)
frontend/    Part 3 — Web dashboard (Go + templ + htmx)      ← PHASE A BUILT
```

Part 2 status: `/sync` API + schema/migrations + Docker/Tailscale stack + Grafana dashboards +
`distance_km`/300k views + daily pg_dump backups (→iCloud) + ntfy alerts (Core 4 + DTC) are built
and running on the laptop-as-temp-server. Still pending: Claude API analysis, rclone backup push
(on the real server), and provisioning the real home server.

Part 3 status: Phase A built — server-rendered car-health app (`web` container on the tailnet at
:8090): overview, trip history, trip detail, DTC log. Pending: Phase B service records (photo →
Claude vision extraction → logbook) and Phase C AI analysis. Parts are built sequentially.

See `pi/CLAUDE.md` for detailed Pi internals: threading model, OBD connection rules, SQLite thread-safety contract, polling tier PID table, shutdown order, Mode 22 hex addresses, and systemd service specs.

## Commands (pi/)

**Python**: Use `/usr/bin/python3` on this Mac — Homebrew Python 3.14 is broken. Venv is at `pi/venv/`.

```bash
# Install dev dependencies (first time or after pulling)
cd pi && /usr/bin/python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# Run all tests with coverage (venv must be active)
cd pi && source venv/bin/activate && /usr/bin/python3 -m pytest tests/ --cov=src

# Run a single test file
cd pi && source venv/bin/activate && /usr/bin/python3 -m pytest tests/test_collector.py -v

# Run a single test by name
cd pi && source venv/bin/activate && /usr/bin/python3 -m pytest tests/test_trip.py::test_trip_start_requires_voltage_and_rpm -v
```

Tests run without hardware. `conftest.py` sets required env vars before any `src/` import and inserts `src/` into `sys.path` — no install step needed.

## Operational CLI (Pi — `jarvis`)

`pi/scripts/jarvis` is installed to PATH on the Pi. Run `jarvis help` for the full list. Key commands:

```bash
jarvis status          # systemd status for rfcomm, collector, and sync timer
jarvis logs            # tail live OBD log (Ctrl+C to stop)
jarvis journal [n]     # collector logs via journald (works without USB mounted)
jarvis sync            # trigger sync now and show result
jarvis rows            # row counts + unsynced per table
jarvis trips [n]       # last n trips with duration
jarvis ford [n]        # latest Mode 22 readings (TCM/PCM/misfires)
jarvis dtc             # recent DTC fault codes
jarvis health          # CPU temp, memory, disk, uptime, restart count
jarvis bt              # Bluetooth dongle + RFCOMM connection status
jarvis update          # git pull + restart collector
jarvis datasette start # start Datasette browser at :8001 (on-demand only)
```

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
- Trip start: voltage >13V AND RPM>0 — Trip end (any of): RPM=0 for >5s AND a fresh <12.5V reading (fast key-off); RPM=0 for >30s AND engine-off otherwise confirmed (silent voltage PID); or an independent watchdog force-ends a trip after >5min with no RPM>0 (back-dated to last activity) — this catches a frozen OBD callback stream when Bluetooth drops at key-off, which would otherwise merge the next drive into the open trip
- No polling pause — all tiers record continuously during an active trip (idle/ESS-stop data is kept; filter on `rpm > 0` in Grafana when an average should exclude idle)
- NULL stored for bad PID responses — never carry forward last known value
- SQLite mirrors PostgreSQL structure exactly — same tables, same columns, same units
- UUID per row for sync deduplication — `ON CONFLICT (id) DO NOTHING` on all inserts (trips is the one upsert: `DO UPDATE` end_time/duration_s)
- Sync endpoint only over Tailscale + API key header (both required) — no public exposure
- Sync runs **once per drive** (boot-triggered service, not a recurring timer): proactive NetworkManager autoconnect → drain whole backlog → 5-min grace (trailing data + SSH window) → `nmcli device disconnect wlan0` (suppresses autoconnect till reboot). `jarvis net hold/release` keeps the link up for debugging. See `pi/CLAUDE.md`.
- WAL mode on ext4 USB drive — abrupt power cuts are safe, SQLite replays WAL on next open
- All SQLite writes serialised through `QueueWriter`; thread safety via `_db_lock`
- `reconnect_count` and `restart_count` persisted to USB drive files so sync script can read them independently of the collector process

## Critical Non-Obvious Rules (Pi)

**OBD connection must use `fast=False`**: `obd.OBD("/dev/rfcomm0", fast=False, timeout=30)` — omitting it drops the connection on the Pi Bluetooth stack. `obd.Async` must be instantiated with a port string directly, not wrapped around an existing `obd.OBD` object.

**All SQLite access must go through `QueueWriter`**: Use `enqueue()` for INSERT paths, `direct_execute()` for UPDATE/DELETE, and `direct_query()` for reads that run on the OBD callback thread (e.g. `get_trip_number`). Never call `conn.execute()` directly from outside `QueueWriter` — `_db_lock` will not protect it. INSERTs use `ON CONFLICT(id) DO NOTHING` so a re-enqueued row is an idempotent no-op.

**DTC scans must stop the async loop first**: `collector.query_sync()` stops `obd.Async`, issues the synchronous `GET_DTC` query, then restarts the loop. Without this, the async polling thread consumes the DTC response bytes on `/dev/rfcomm0` and the synchronous `query()` call times out. A `_dtc_lock` serialises concurrent DTC scans (e.g. trip-start and trip-end overlapping).

**Collector uses combined rows per table, not one row per PID**: Each PID callback accumulates its value into a per-table buffer (`_table_buffer`). When `interval_s` elapses for that table, the entire buffer is flushed as one combined row via `QueueWriter.enqueue()`. This means `obd_1s` gets one row/second with `rpm`, `speed_kmh`, `throttle_pct`, and `load_pct` all in a single row — not four separate sparse rows. The schema is designed around this.

**Shutdown order is fixed** — DTC threads must finish before disconnect; queue must drain before `conn.close()`:
```
collector.stop() → trip_manager.stop() → queue_writer.stop() → obd_connection.disconnect() → conn.close()
```

**systemd services** (5 total):
- `obd-collector.service` — Type=notify, WatchdogSec=60, Restart=always
- `obd-sync.service` — Type=simple, **boot-triggered** (no timer): runs the once-per-drive sync loop, then `ExecStopPost` disconnects wlan0 (unless `jarvis net hold` flag is set). The old `obd-sync.timer` was removed.
- `rfcomm-connect.service` — binds `/dev/rfcomm0` to the OBDLink MX+ MAC address at boot; uses `Wants=` (not `Requires=`) so `obd-collector.service` starts even if BT binding fails on the first attempt; includes `ExecStartPre=-/usr/bin/rfcomm release 0` to clear any stale binding from a previous session before connecting
- `obd-datasette.service` — on-demand only (not enabled at boot); serves read-only Datasette browser at `:8001`; start via `jarvis datasette start`
- `obd-led.service` — Type=simple, Restart=always; runs `led_status.py`, the status-LED daemon. Reads-only (systemd state + sysfs + read-only DB), fully decoupled from the collector. See `pi/CLAUDE.md` for the two-LED behaviour spec and GPIO pin map.

`obd-collector` and `obd-led` are hardened (`ProtectSystem=strict`, `ProtectHome=read-only`, `NoNewPrivileges`, etc.). **`ReadWritePaths=/mnt/usb` is mandatory on both** — SQLite WAL needs read-write on the DB + `-wal`/`-shm` sidecars, and a WAL *reader* (the LED daemon) must open `-shm` read-write too; dropping it breaks DB access. `StartLimit*` must sit in `[Unit]`, not `[Service]` (systemd silently ignores them there).

**Schema is create-only**: `storage.init_schema()` runs `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS` on every boot — no migration logic. The schema is treated as final; changing a column on an already-deployed DB requires wiping `obd.db` (the data is synced to PostgreSQL, the USB copy is a backup).

## Data Volume
~192,000 rows/month based on 1hr/day Mon–Sat + 4hrs Sunday.
System runs until 300,000 km — not a fixed time horizon.
