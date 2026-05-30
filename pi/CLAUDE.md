# Pi — CLAUDE.md

## Overview
Python OBD collector running on Raspberry Pi 3B in the car.
Collects OBD data → stores in SQLite → syncs to home server via iPhone hotspot.

## Hardware
- Raspberry Pi 3B (Kano kit)
- OBDLink MX+ via Bluetooth (USB BT dongle TP-Link UB500 — NOT onboard BT)
- SQLite on USB flash drive at /mnt/usb/data/obd.db
- OS on Samsung Pro Endurance microSD
- DS3231 RTC module (I2C) + fake-hwclock as fallback
- Car's built-in USB port for power

## Python Stack
- python-obd 0.7.3 (async mode)
- SQLite via sqlite3 (stdlib)
- systemd watchdog via sd_notify
- psutil, smbus2, sdnotify, requests

## Running Tests
```bash
/usr/bin/python3 -m pytest pi/tests/ --cov=pi/src
```
Dev dependencies: `pip install -r pi/requirements-dev.txt`
199 tests, 97% coverage. All tests run without hardware.

## OBD Connection
Always use:
```python
obd.OBD("/dev/rfcomm0", fast=False, timeout=30)
```
`fast=False` is required on Pi — without it the Pi Bluetooth stack drops the connection.

`obd.Async` must be instantiated with a port string directly, not by wrapping an existing
`obd.OBD` object. Collector opens its own `obd.Async` independently.

## Polling Tiers
| Table | Interval | PIDs |
|-------|----------|------|
| obd_1s | 1s | rpm, speed_kmh, throttle_pct, load_pct |
| obd_5s | 5s | coolant_temp_c, oil_temp_c, intake_air_temp_c, maf_gs, map_kpa, baro_pressure_kpa, stft_pct, ltft_pct, o2_b1s1_v, o2_b1s2_v, timing_advance_deg |
| obd_30s | 30s | battery_v, fuel_level_pct, ambient_air_temp_c, distance_since_dtc_cleared_km |
| ford_obd_5s | 5s (TBC) | trans_temp_c, trans_gear, tcc_ratio |
| ford_obd_10s | 10s (TBC) | knock_retard_deg, boost_desired_psi, boost_actual_psi, wastegate_pct |
| ford_obd_20s | 20s (TBC) | misfire_cyl1–4, fuel_rail_pressure_psi |

Ford tables are empty stubs — populate after FORScan baseline scan confirms hex addresses.

Polling tiers are implemented via a single `obd.Async` connection. python-obd 0.7.3 does
not support per-watcher intervals — all PIDs fire at ~1Hz. Each callback uses a
time-filter (`time.monotonic()`) to skip enqueue until `interval_s` has elapsed.

## Trip Detection
- Trip start: battery_v > 13.0V AND rpm > 0 (both required)
- Trip end: rpm = 0 for >30s AND battery_v < 12.5V (both required)
- Polling pause: obd_1s + obd_5s suppressed when rpm=0 for >30s; obd_30s keeps running
- Pause is wired to `TripManager.is_paused` — checked in every `_make_callback()` closure

## Threading Model
```
python-obd async thread
    └── on_rpm() / on_voltage() callbacks → TripManager (protected by _lock)
    └── PID data callbacks → QueueWriter.enqueue() (non-blocking, puts on queue)

QueueWriter._drain thread
    └── batches 30 rows or 2s → _flush() → conn.execute() + commit()
    └── all conn access protected by _db_lock

DTC scan threads (daemon, one per trip boundary)
    └── obd_connection.connection.query(GET_DTC)
    └── joined by TripManager.stop() before disconnect

obd-monitor thread (Collector)
    └── checks is_connected() every 10s
    └── calls obd_connection.reconnect() on drop, restarts obd.Async
```

**SQLite thread safety rule**: all `conn.execute()` + `conn.commit()` must go through
either `QueueWriter.enqueue()` (INSERT path) or `QueueWriter.direct_execute()` (UPDATE
path). Never call `conn.execute()` directly from outside QueueWriter — `_db_lock` will
not protect it.

## SQLite Schema Principles
- UUID primary keys (TEXT in SQLite)
- ISO8601 timestamps as TEXT
- Units in column names: coolant_temp_c, battery_v, speed_kmh
- WAL mode enabled: `PRAGMA journal_mode=WAL` — check return value, warn if not "wal"
- `PRAGMA synchronous=NORMAL` + `PRAGMA cache_size=-8000` on every open
- `PRAGMA integrity_check` on every open — rename .corrupt and start fresh on failure
- `synced INTEGER DEFAULT 0` on every table
- NULL stored for bad/missing PID responses — never carry forward

## Persistent Files on USB Drive
| File | Written by | Read by | Purpose |
|------|-----------|---------|---------|
| `/mnt/usb/data/obd.db` | collector | sync | primary data store |
| `/mnt/usb/data/restart_count` | main.py on boot | sync (health snapshot) | collector restart counter |
| `/mnt/usb/data/reconnect_count` | obd_connection.reconnect() | sync (health snapshot) | BT reconnect counter |
| `/mnt/usb/logs/obd.log` | logger | health.py (tail) | rotating log (5MB × 7) |

Both counter files are `fsync`'d after every write — engine off = immediate power cut.

## Sync
- Fires 5 min after boot via systemd timer, then every 5 min
- Two-step network check: wlan0 has IP (hotspot) → ping Tailscale IP (server reachable)
- POSTs unsynced rows (`WHERE synced=0`) to Golang API in priority order: obd_1s first, trips last
- Auth: Bearer token in Authorization header
- Config: `/etc/obd-collector/config.env` (never committed to git)
- Marks `synced=1` after successful POST — retries with backoff on `OperationalError`
- Max 1000 batches per table per run to guard against stuck-loop
- Pi health snapshot included in every sync payload (reads reconnect_count from file)
- Ford tables skipped silently at DEBUG level if they don't exist yet

## Logging
Log file: `/mnt/usb/logs/obd.log` (5MB × 7 rotating files = 35MB cap)
Falls back to stderr-only if USB not mounted — boot WARNING always appears in journald.

Log these events:
- Pi boot (WARNING level so it always appears in journald)
- OBD connection established / reconnected
- First PID read success
- Trip start / trip end
- BT reconnect (with total reconnect count)
- Polling paused / resumed
- Sync success (with row count), skipped (no hotspot), failed (with error)
- SQLite write errors
- Watchdog ping sent
- Script restart (from systemd)
- DS3231 OSF flag set on boot

Do NOT log: individual PID values, every poll cycle, WAL checkpoints.

## systemd Services
- `obd-collector.service` — Type=notify, Restart=always, RestartSec=15, WatchdogSec=60
  - `TimeoutStartSec=300` — OBD init can take multiple retry cycles
  - `TimeoutStopSec=60` — covers 30s OBD timeout + 15s queue drain before SIGKILL
- `obd-sync.service` — Type=oneshot, TimeoutStartSec=120
- `obd-sync.timer` — OnBootSec=5min, OnUnitActiveSec=5min

## Shutdown Sequence (main.py finally block)
```
collector.stop()      → stops obd.Async loop + monitor thread
trip_manager.stop()   → joins in-flight DTC scan threads (5s timeout)
queue_writer.stop()   → drains queue + commits remaining rows (15s timeout)
obd_connection.disconnect()
conn.close()
```
Order matters — DTC threads must finish before disconnect; queue must drain before close.

## Mode 22 Ford PIDs (confirmed hex addresses)
| PID | Parameter | Formula |
|-----|-----------|---------|
| 22F45C | Oil Temp | (A-40)*1.8+32 → convert to C |
| 220318 | Knock Retard | Signed(A)/16 degrees |
| 22033E | Desired Boost | ((256*A)+B)/128*0.145-14.7 PSI |
| 22D137 | Actual Boost (TBC formula) | TBD |
| 2203CA | Wastegate | raw % |
| 221E1C | Trans Fluid Temp | ([A:B]*0.1125)+32 → convert to C |
| 221E12 | Current Gear | A |
| 221E15 | TCC Ratio | [A:B]/4096 |

Misfire via Mode 06: 06A20C–06A50C (cylinders 1–4).
Fuel rail pressure hex address unconfirmed — verify via FORScan.
