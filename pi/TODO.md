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

## 2. Configuration (`config.py`)
- [ ] Read from `/etc/obd-collector/config.env`
- [ ] Required values: API_URL, API_KEY, TAILSCALE_IP, OBD_PORT, SYNC_BATCH_SIZE, DB_PATH, LOG_PATH
- [ ] Fail fast on missing required config — log error and exit cleanly

---

## 3. Logging (`logger.py`)
- [ ] Rotating file handler — 5MB × 7 files at LOG_PATH
- [ ] Log format: `timestamp | level | message`
- [ ] Single logger instance imported by all modules
- [ ] Log on import: Pi boot message with timestamp and Python version

---

## 4. DS3231 RTC Check (`main.py`)
- [ ] Read DS3231 OSF flag via I2C on boot
- [ ] If OSF set → log WARNING: "RTC battery may be dead, timestamp accuracy not guaranteed"
- [ ] If DS3231 not found → log WARNING: "DS3231 not found, relying on fake-hwclock"
- [ ] Non-fatal — continue boot regardless

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

## 6. Queue Writer (`queue_writer.py`)
- [ ] Thread-safe `queue.Queue` instance
- [ ] Main thread drain loop — dequeue and write to correct SQLite table
- [ ] Handle SQLite write errors — log, do not crash
- [ ] Expose `enqueue(table_name, row_dict)` used by all callbacks
- [ ] Drain remaining queue before shutdown

---

## 7. OBD Commands (`obd_commands.py`)
- [ ] Standard Mode 01 PID definitions (verify python-obd has all needed)
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

## 8. OBD Connection (`obd_connection.py`)
- [ ] Connect: `obd.OBD("/dev/rfcomm0", fast=False, timeout=30)`
- [ ] Retry every 15s on failure — log each attempt with count
- [ ] Log success with device name
- [ ] Expose reconnect method for mid-trip BT drop recovery
- [ ] Track reconnect count for Pi health payload

---

## 9. Async Collector (`collector.py`)
- [ ] Set up `obd.Async` connection
- [ ] Register standard PID watchers:
  - 1s: RPM, speed, throttle, load
  - 5s: coolant, oil temp, MAF, STFT, LTFT, O2 B1S1, O2 B1S2
  - 30s: battery voltage, fuel level
- [ ] Register Ford Mode 22 watchers:
  - ford_obd_5s, ford_obd_10s, ford_obd_20s tiers
- [ ] Each callback: build row dict (UUID, trip_id, timestamp, value) → enqueue
- [ ] NULL enqueued if value is None
- [ ] Handle connection drop → trigger reconnect → log

---

## 10. Trip Detection (`trip.py`)
- [ ] Trip start: `battery_v > 13.0` AND `rpm > 0`
  - Generate new trip_id (UUID)
  - Log "Trip started: {trip_id}"
  - Write to `trips` table
  - Trigger DTC scan
- [ ] Trip end: `rpm = 0` for >30s AND `battery_v < 12.5`
  - Log "Trip ended: {trip_id}"
  - Update `trips.end_time`
  - Trigger DTC scan
- [ ] Polling pause: `rpm = 0` for >30s → pause obd_1s + obd_5s watchers
- [ ] Polling resume: `rpm > 0` → resume watchers
- [ ] obd_30s always running regardless of RPM
- [ ] BT drop mid-trip → keep same trip_id after reconnect

---

## 11. DTC Scanner (`trip.py`)
- [ ] Run `GET_DTC` on trip start and trip end
- [ ] Write each code to `dtc_events` (UUID, trip_id, timestamp, code, description, status)
- [ ] Log each DTC: "DTC detected: {code} — {description}"
- [ ] Log if clean: "DTC scan clean"

---

## 12. Pi Health Metrics (`health.py`)
- [ ] CPU temp: `/sys/class/thermal/thermal_zone0/temp`
- [ ] Memory free: `psutil.virtual_memory().available`
- [ ] Disk free: `psutil.disk_usage('/mnt/usb').free`
- [ ] OBD reconnect count: from `obd_connection.py`
- [ ] Restart count: persistent counter at `/mnt/usb/data/restart_count` — increment on every boot
- [ ] Last error: last ERROR log line
- [ ] Rows collected since last sync: count from SQLite
- [ ] Write snapshot to `pi_health_log`

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
