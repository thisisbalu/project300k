# Pi Collector — Coding TODO

## 1. Project Structure Setup ✓
- [x] Create `pi/src/` folder with all modules:
  - `main.py` — entry point, boot sequence, main loop
  - `config.py` — load and validate config from env file
  - `obd_connection.py` — OBD connection, reconnect logic
  - `obd_commands.py` — standard + Ford Mode 22 + Mode 06 PID definitions
  - `collector.py` — async watcher registration, callbacks
  - `trip.py` — trip start/end detection, polling pause logic
  - `storage.py` — SQLite schema creation, connection, indexes
  - `queue_writer.py` — thread-safe queue, main thread SQLite writer
  - `health.py` — Pi health metrics collection
  - `sync.py` — network check, per-table batch sync to server
  - `logger.py` — rotating file logger, shared instance
- [x] Create `pi/systemd/` folder:
  - `obd-collector.service`
  - `obd-sync.service`
  - `obd-sync.timer`
- [x] Create `pi/scripts/install.sh` — venv setup, systemd install, service enable
- [x] Create `pi/requirements.txt` with pinned versions (python-obd==0.7.3, requests, psutil)

---

## 2. Configuration (`config.py`) ✓
- [x] Read from `/etc/obd-collector/config.env`
- [x] Required values: API_URL, API_KEY, TAILSCALE_IP, OBD_PORT, SYNC_BATCH_SIZE, DB_PATH, LOG_PATH
- [x] Fail fast on missing required config — log error and exit cleanly

---

## 3. Logging (`logger.py`) ✓
- [x] Rotating file handler — 5MB × 7 files at LOG_PATH
- [x] Log format: `timestamp | level | message`
- [x] Single logger instance imported by all modules
- [x] Log on import: Pi boot message with timestamp and Python version

---

## 4. DS3231 RTC Check (`main.py`) ✓
- [x] Read DS3231 OSF flag via I2C on boot
- [x] If OSF set → log WARNING: "RTC battery may be dead, timestamp accuracy not guaranteed"
- [x] If DS3231 not found → log WARNING: "DS3231 not found, relying on fake-hwclock"
- [x] Non-fatal — continue boot regardless

---

## 5. SQLite Schema (`storage.py`)
- [ ] Connect to DB_PATH
- [ ] Enable WAL mode: `PRAGMA journal_mode=WAL`
- [ ] Enable foreign keys: `PRAGMA foreign_keys=ON`
- [ ] Create tables if not exist:
  - `trips`
  - `obd_1s`
  - `obd_5s`
  - `obd_30s`
  - `ford_obd_5s`
  - `ford_obd_10s`
  - `ford_obd_20s`
  - `dtc_events`
  - `pi_health_log`
- [ ] Create indexes on `timestamp`, `trip_id`, `synced` per table
- [ ] `schema_version` table for future migrations

---

## 6. Queue Writer (`queue_writer.py`) ✓
- [x] Thread-safe `queue.Queue` instance
- [x] Main thread drain loop — dequeue and write to correct SQLite table
- [x] Handle SQLite write errors — log, do not crash
- [x] Expose `enqueue(table_name, row_dict)` used by all callbacks
- [x] Drain remaining queue before shutdown

---

## 7. OBD Commands (`obd_commands.py`)
- [x] Standard Mode 01 PID definitions — all verified in python-obd 0.7.3
- [x] PIDConfig dataclass — carries command, table, column, interval_s
- [x] ALL_PIDS flat list — single source of truth consumed by collector.py
- [ ] Custom Mode 22 Ford OBDCommand objects (after FORScan confirmation):
  - `22F45C` — oil temp
  - `220318` — knock retard
  - `22033E` — desired boost
  - `22D137` — actual boost (formula TBD after FORScan)
  - `2203CA` — wastegate
  - `221E1C` — trans fluid temp
  - `221E12` — trans gear
  - `221E15` — TCC ratio
  - Additional PIDs after FORScan scan
- [ ] Mode 06 misfire commands: `06A20C`–`06A50C` (cylinders 1–4)
- [ ] Each decoder returns `None` on parse error → stored as NULL

---

## 8. OBD Connection (`obd_connection.py`) ✓
- [x] Connect: `obd.OBD("/dev/rfcomm0", fast=False, timeout=30)`
- [x] Retry every 15s on failure — log each attempt with count
- [x] Log success with device name
- [x] Expose reconnect method for mid-trip BT drop recovery
- [x] Track reconnect count for Pi health payload

---

## 9. Async Collector (`collector.py`) ✓
- [x] Set up `obd.Async` connection
- [x] Register all PIDs from ALL_PIDS in obd_commands.py — single loop
- [x] Register RPM + voltage watchers for TripManager trip detection
- [x] Each callback: build row dict (UUID, trip_id, timestamp, value) → enqueue
- [x] NULL enqueued if value is None
- [ ] Handle connection drop → trigger reconnect → log

---

## 10. Trip Detection (`trip.py`) ✓
- [x] Trip start: `battery_v > 13.0` AND `rpm > 0`
- [x] Generate new trip_id (UUID), write to trips table, trigger DTC scan
- [x] Trip end: `rpm = 0` for >30s AND `battery_v < 12.5`
- [x] Update trips.end_time, trigger DTC scan, clear trip_id
- [x] Polling pause: `rpm = 0` for >30s → log pause (watcher pause in collector TBD)
- [x] Polling resume: `rpm > 0` → log resume
- [x] Monotonic clock for RPM=0 timer — avoids NTP clock jump false triggers
- [x] BT drop mid-trip → same trip_id kept after reconnect

---

## 11. DTC Scanner (`trip.py`) ✓
- [x] Run `GET_DTC` on trip start and trip end
- [x] Write each code to `dtc_events` (UUID, trip_id, timestamp, code, description, status)
- [x] Log each DTC: "DTC detected: {code} — {description}"
- [x] Log if clean: "DTC scan clean"
- [x] Skip gracefully if OBD not connected — best-effort, non-fatal

---

## 12. Pi Health Metrics (`health.py`) ✓
- [x] CPU temp: `/sys/class/thermal/thermal_zone0/temp`
- [x] Memory free: `psutil.virtual_memory().available`
- [x] Disk free: `psutil.disk_usage('/mnt/usb').free`
- [x] OBD reconnect count: passed in from OBDConnection
- [x] Restart count: persistent counter at `/mnt/usb/data/restart_count` — increment on every boot
- [x] Last error: last ERROR line scanned from log file
- [ ] Rows collected since last sync — deferred to Task 13 (sync script has the context)

---

## 13. Sync Script (`sync.py`)
- [ ] Step 1 — Network check:
  - Check wlan0 has IP → log "Sync skipped — no hotspot" if not
  - Ping TAILSCALE_IP → log "Sync skipped — server unreachable" if fail
- [ ] Step 2 — Collect and write Pi health snapshot
- [ ] Step 3 — Per-table sync in priority order:
  - `obd_1s` → `obd_5s` → `obd_30s` → `ford_obd_5s` → `ford_obd_10s` → `ford_obd_20s` → `dtc_events` → `pi_health_log` → `trips`
  - Read batch of SYNC_BATCH_SIZE rows WHERE synced=0
  - POST to API_URL with Authorization: Bearer {API_KEY}
  - On HTTP 200 → mark rows synced=1
  - On failure → log error, stop table, move to next
  - Repeat until no unsynced rows
- [ ] Step 4 — Log sync summary: "Sync complete — {rows} rows across {tables} tables"

---

## 14. systemd Services (`systemd/`)
- [ ] `obd-collector.service`:
  - Restart=always, RestartSec=15
  - StartLimitBurst=5, StartLimitIntervalSec=60
  - WatchdogSec=60
  - After=bluetooth.target
- [ ] `obd-sync.service`: Type=oneshot
- [ ] `obd-sync.timer`: OnBootSec=5min, OnUnitActiveSec=5min
- [ ] `scripts/install.sh`:
  - Create venv at `pi/venv/`
  - Install requirements
  - Copy systemd files to `/etc/systemd/system/`
  - Enable and start services

---

## 15. Watchdog Integration (`main.py`)
- [ ] Send `WATCHDOG=1` ping every 30s from main loop
- [ ] Log "Watchdog ping sent" on each ping
- [ ] Main loop stall → systemd restarts service automatically

---

## 16. Testing
- [ ] OBD connection on bench — confirm rfcomm0 connects
- [ ] Each standard PID returns sane value
- [ ] Mode 22 Ford PIDs — confirm values after FORScan verification
- [ ] SQLite writes — rows land in correct tables with correct columns
- [ ] Queue writer — no rows lost under rapid callback firing
- [ ] Trip detection — simulate voltage + RPM changes
- [ ] Polling pause — confirm 1s/5s stop, 30s continues when RPM=0
- [ ] BT reconnect — kill BT, confirm reconnect and same trip_id maintained
- [ ] DTC scan — confirm codes written to dtc_events
- [ ] Sync script — rows POST and marked synced=1
- [ ] Network detection — test all three failure modes
- [ ] systemd auto-restart — kill process, confirm restart within 15s
- [ ] Hardware watchdog — freeze main loop, confirm Pi reboots
- [ ] Full pipeline end to end — drive, park, next boot, verify data on server
