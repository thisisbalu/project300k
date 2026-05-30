# Real-World Testing Checklist

Complete hardware setup (HARDWARE_SETUP.md) before starting.
Work through phases in order — each phase is a gate for the next.

---

## Phase 1 — BT Connection (engine on, Pi booted, OBDLink plugged in)

- [ ] `/dev/rfcomm0` exists: `ls -la /dev/rfcomm0`
- [ ] hci0 (USB dongle) is active: `hciconfig hci0`
- [ ] OBDLink MX+ shows as trusted: `bluetoothctl -- info <MAC>`
- [ ] python-obd connects:
  ```bash
  venv/bin/python3 -c "
  import obd
  c = obd.OBD('/dev/rfcomm0', fast=False, timeout=30)
  print('Connected:', c.is_connected())
  print('Protocol:', c.protocol_name())
  print('RPM:', c.query(obd.commands.RPM))
  c.close()
  "
  ```
- [ ] `Connected: True`
- [ ] Protocol shows a CAN protocol (e.g. `ISO 15765-4 (CAN 11/500)`)
- [ ] RPM returns a non-null value matching the tachometer

**If this phase fails:** check rfcomm binding, BT pairing trust, `fast=False` flag, engine is running.

---

## Phase 2 — Collector Smoke Test (engine on, run manually)

Run the collector manually so all output is visible:

```bash
cd /home/pi/project300k/pi
sudo systemctl stop obd-collector  # stop service if running
venv/bin/python3 src/main.py
```

Watch the log in a second terminal: `tail -f /mnt/usb/logs/obd.log`

### Boot sequence
- [ ] `Pi boot — Python 3.x` appears in log
- [ ] `DS3231 RTC OK` (or warning if coin cell issue)
- [ ] `restart_count: N` increments each boot
- [ ] `OBD connection established — port: /dev/rfcomm0`

### Trip start (engine on, alternator charging)
- [ ] `Trip started: <uuid>` appears within a few seconds of engine on
- [ ] All PIDs registered: `Watching PID: RPM → obd_1s.rpm every 1s` (×15 lines)
- [ ] `QueueWriter started`
- [ ] `READY=1` sent (sdnotify)
- [ ] `Watchdog ping sent` every 30s

### Data writing
- [ ] No `SQLite write error` lines
- [ ] No `QueueWriter full` lines
- [ ] No `Rejected write to unknown table` lines

### Trip end (engine off, wait 30s, voltage drops)
- [ ] `Polling paused — RPM=0 for >30s` appears
- [ ] `Trip ended: <uuid>` appears after ~35s of engine off
- [ ] `DTC scan clean (trip_end) — no fault codes` (clean car)

### Clean shutdown (Ctrl+C)
- [ ] `Shutting down — draining queue`
- [ ] `TripManager stopped`
- [ ] `QueueWriter stopped`
- [ ] `OBD connection closed`
- [ ] `obd-collector stopped`

**If this phase fails:** check log for the specific error line. Common issues: wrong DB_PATH, USB not mounted, config.env not filled in.

---

## Phase 3 — Data Sanity Check (after a 20+ minute drive)

SSH into Pi after a drive, inspect SQLite:

```bash
sqlite3 /mnt/usb/data/obd.db
```

### Trip recorded
- [ ] `SELECT trip_number, start_time, end_time, duration_s FROM trips;`
  - [ ] `end_time` is not NULL (trip ended cleanly)
  - [ ] `duration_s` matches approximate drive time
  - [ ] `trip_number` increments per drive

### Row counts look right
- [ ] `SELECT COUNT(*) FROM obd_1s;` — roughly `duration_s × 1` rows
- [ ] `SELECT COUNT(*) FROM obd_5s;` — roughly `duration_s / 5` rows
- [ ] `SELECT COUNT(*) FROM obd_30s;` — roughly `duration_s / 30` rows

### Values are in sane ranges

```sql
SELECT timestamp, rpm, speed_kmh, throttle_pct, load_pct FROM obd_1s LIMIT 20;
```
- [ ] `rpm`: 600–900 at idle, 1000–4000 while driving
- [ ] `speed_kmh`: 0 at idle, matches speedometer while driving
- [ ] `throttle_pct`: 0–100, responds to pedal
- [ ] `load_pct`: 20–40 idle, higher under acceleration

```sql
SELECT timestamp, coolant_temp_c, oil_temp_c, stft_pct, ltft_pct FROM obd_5s LIMIT 20;
```
- [ ] `coolant_temp_c`: rises from ambient to 85–100°C on a warm engine
- [ ] `oil_temp_c`: rises slower than coolant, reaches 90–110°C
- [ ] `stft_pct`: between -10 and +10 (fuel trim healthy)
- [ ] `ltft_pct`: between -10 and +10 (learned trim healthy)

```sql
SELECT timestamp, battery_v, fuel_level_pct FROM obd_30s LIMIT 20;
```
- [ ] `battery_v`: 13.8–14.4V while engine running (alternator charging)
- [ ] `fuel_level_pct`: matches fuel gauge, stable per trip

### NULL rate is acceptable
```sql
SELECT 
  COUNT(*) as total_rows,
  SUM(CASE WHEN rpm IS NULL THEN 1 ELSE 0 END) as null_rpm,
  SUM(CASE WHEN speed_kmh IS NULL THEN 1 ELSE 0 END) as null_speed
FROM obd_1s;
```
- [ ] NULL rate < 5% for core PIDs (rpm, speed, coolant_temp_c)
- [ ] Spike in NULLs = BT glitch (acceptable if brief)

### DTC events
```sql
SELECT * FROM dtc_events;
```
- [ ] Empty = no fault codes (expected on healthy car)
- [ ] If codes present — cross-check with OBD app (Torque, OBD Fusion)

**Cross-check values against a known-good OBD app on the same drive.**

---

## Phase 4 — Sync Test (iPhone hotspot connected)

- [ ] Connect iPhone hotspot
- [ ] Wait 5 minutes, or trigger manually: `sudo systemctl start obd-sync.service`
- [ ] Check sync log:
  ```bash
  journalctl -u obd-sync.service -n 50
  ```
  - [ ] `Sync started`
  - [ ] `Synced N rows from obd_1s`
  - [ ] `Sync complete — N rows synced`
- [ ] Confirm rows marked synced in SQLite:
  ```bash
  sqlite3 /mnt/usb/data/obd.db "SELECT COUNT(*) FROM obd_1s WHERE synced=0;"
  # Should be 0
  ```
- [ ] Confirm rows landed on server (check PostgreSQL on home server)
- [ ] Health snapshot in `pi_health_log`:
  ```bash
  sqlite3 /mnt/usb/data/obd.db "SELECT * FROM pi_health_log ORDER BY timestamp DESC LIMIT 1;"
  ```
  - [ ] `usb_drive_mounted = 1`
  - [ ] `bt_adapter_present = 1`
  - [ ] `rtc_ok = 1`
  - [ ] `cpu_temp_c` is a real number (35–70°C typical)

---

## Phase 5 — Resilience Tests (spread over first week)

### Auto-restart on crash
- [ ] Collector is running via systemd
- [ ] `sudo kill -9 $(pgrep -f main.py)`
- [ ] Wait 15s (RestartSec=15)
- [ ] `sudo systemctl status obd-collector` shows `active (running)`
- [ ] `cat /mnt/usb/data/restart_count` incremented

### BT reconnect mid-trip
- [ ] Engine on, collector running, trip active
- [ ] `sudo rfcomm release 0` (simulate BT drop)
- [ ] Wait ~15s
- [ ] Log shows: `Async OBD connection dropped — reconnecting`
- [ ] Log shows: `OBD reconnected (total reconnects this session: 1)`
- [ ] `cat /mnt/usb/data/reconnect_count` shows 1
- [ ] Same trip_id continues in obd_1s (no new trip started)
- [ ] Gap in obd_1s timestamps visible — honest, expected

### Polling pause at idle
- [ ] Engine idling for 35+ seconds (at a drive-through or long red light)
- [ ] Log shows: `Polling paused — RPM=0 for >30s (30s tier still active)`
- [ ] Engine revs — log shows: `Polling resumed — RPM > 0`
- [ ] Check obd_1s: gap in rows during pause period, then resumes
- [ ] Check obd_30s: continues writing during pause (battery_v rows present)

### Power cut recovery (WAL test)
- [ ] While collector is writing: `sudo kill -9 $(pgrep -f main.py)`
- [ ] `sqlite3 /mnt/usb/data/obd.db "PRAGMA integrity_check;"` → must return `ok`
- [ ] Restart collector — starts clean, no corruption error in log

### Watchdog fires on hang (optional — simulates deadlock)
- [ ] `sudo kill -STOP $(pgrep -f main.py)` (suspend process)
- [ ] Wait 60s (WatchdogSec=60)
- [ ] systemd kills and restarts the service automatically
- [ ] `sudo systemctl status obd-collector` shows `active (running)`

---

## Phase 6 — First Full Drive Verification

After all phases pass, do a normal 30+ minute drive with the system running under systemd.

- [ ] No manual intervention during the drive
- [ ] After drive, SSH in and check:
  ```bash
  tail -50 /mnt/usb/logs/obd.log
  sqlite3 /mnt/usb/data/obd.db "SELECT trip_number, start_time, end_time, duration_s FROM trips ORDER BY trip_number DESC LIMIT 5;"
  ```
- [ ] Trip start and end times match the actual drive
- [ ] Row counts per table are proportional to drive duration
- [ ] No errors in log
- [ ] Next sync cycle pushes all rows to server
- [ ] Data visible in Grafana (once backend is deployed)

**Part 1 real-world testing complete when all phases pass on two consecutive drives.**
