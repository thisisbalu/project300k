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
- gpiozero (status LEDs) — the `lgpio` pin-factory backend is a C extension installed on the Pi only via `install.sh`, never in `requirements.txt` (keeps Mac dev installs clean). `led_status.py` imports `gpiozero` inside `LedDriver` so the module imports under pytest without a GPIO backend.

## Running Tests
```bash
cd pi && source venv/bin/activate && /usr/bin/python3 -m pytest tests/ --cov=src
```
`pytest.ini` sets `testpaths = tests`, so run from `pi/`. Dev deps: `pip install -r requirements-dev.txt`.
324 tests, ~94% coverage. All tests run without hardware (gpiozero is mocked).

## Provisioning the Pi (`scripts/install.sh`)
One-shot installer, run on the Pi from a checked-out repo at `/home/balu/project300k`. Idempotent — safe to re-run. It:
- creates `/etc/obd-collector/`, `/mnt/usb/data/`, `/mnt/usb/logs/`
- writes `/etc/obd-collector/config.env` with `REPLACE_ME` placeholders **only if absent** (never overwrites a real config). Required keys: `API_URL`, `API_KEY` (`openssl rand -hex 32`), `TAILSCALE_IP`. The collector exits on boot if any required value is missing.
- builds `venv/`, installs `requirements.txt` + `datasette`
- symlinks `scripts/jarvis` → `/usr/local/bin/jarvis`
- copies all 5 systemd units into `/etc/systemd/system/` and enables `rfcomm-connect`, `obd-collector`, `obd-sync.timer`, `obd-datasette` (collector first connection can take up to `TimeoutStartSec=300`)

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
| obd_5s | 5s | coolant_temp_c, oil_temp_c, intake_air_temp_c, maf_gs, map_kpa, baro_pressure_kpa, stft_pct, ltft_pct, o2_b1s1_v, o2_b1s2_v, timing_advance_deg, fuel_rail_kpa |
| obd_30s | 30s | battery_v, fuel_level_pct, ambient_air_temp_c, distance_since_dtc_cleared_km |
| ford_obd_5s | 5s | trans_temp_c, trans_oil_temp2_c, trans_line_pressure_kpa, trans_gear, tcc_ratio |
| ford_obd_10s | 10s | oil_pressure_kpa, knock_retard_deg, boost_desired_psi, boost_actual_psi, cac_temp_c, wastegate_pct, vct_intake_deg, vct_exhaust_deg |
| ford_obd_20s | 20s | misfire_acc_cyl1–4 |

Ford 5S and 10S addresses confirmed via pid_log_20260605_190444.txt. Ford 20S misfire addresses confirmed 2026-06-06 via Mode 06 scan — TIDs 06A2–06A5, OBDMID 0x0B. Fuel rail pressure address still not found.

Polling tiers are implemented via a single `obd.Async` connection. python-obd 0.7.3 does
not support per-watcher intervals — all PIDs fire at ~1Hz. Each callback uses a
time-filter (`time.monotonic()`) to skip enqueue until `interval_s` has elapsed.

## Trip Detection
- Trip start: battery_v > 13.0V AND rpm > 0 (both required)
- Trip end — any of three (`VOLTAGE_DROP_DURATION_S`/`RPM_ZERO_DURATION_S`/`TRIP_WATCHDOG_TIMEOUT_S`):
  1. **Fast key-off**: rpm = 0 for >5s AND a fresh battery_v < 12.5V (alternator definitively stopped — a long red light holds ~13.8V, so a real drop is unambiguous)
  2. **Standard**: rpm = 0 for >30s AND engine-off otherwise confirmed (covers the silent-voltage-PID case: no fresh reading for >30s)
  3. **Watchdog** (independent thread, `start()`/`stop()`): force-ends a trip after >5min with no rpm>0, **back-dated to last activity** and skipping the trip-end DTC scan. This is the only path that can fire when the OBD/Bluetooth link drops at key-off and freezes the callback stream (rpm reads *nothing*, not 0, so paths 1–2 never run). Without it the next drive merges into the still-open trip (observed: 2/60 trips at 30–45h). `_last_activity_mono`/`_last_activity_wall` track the last rpm>0.
- `_last_voltage`/`_last_voltage_mono` (and `_last_activity_*`) are reset to None in `_end_trip()` — TripManager outlives key-off, so a stale in-drive voltage left behind could start the next trip early (on_rpm fires before on_voltage within a poll)
- No polling pause — every tier records continuously while a trip is active. Idle and ESS auto-stop samples (rpm=0 with the bus alive) are kept on purpose; they're honest data and storage is effectively free. The only guard in `_handle_response` is the active-trip check (`current_trip_id is None` → skip).

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

trip-watchdog thread (daemon, TripManager.start())
    └── every 30s: force-ends an open trip idle >5min (callback-independent)
    └── signalled + joined by TripManager.stop()

obd-monitor thread (Collector)
    └── checks is_connected() every 10s
    └── calls obd_connection.reconnect() on drop, restarts obd.Async
```

**SQLite thread safety rule**: all `conn.execute()` + `conn.commit()` must go through
`QueueWriter.enqueue()` (INSERT path), `QueueWriter.direct_execute()` (UPDATE/DELETE),
or `QueueWriter.direct_query()` (locked read, e.g. `get_trip_number` on the callback
thread). Never call `conn.execute()` directly from outside QueueWriter — `_db_lock`
will not protect it. INSERTs carry `ON CONFLICT(id) DO NOTHING` (idempotent re-enqueue).

`write_failing` only trips when the **whole** batch is unwritable (commit threw, or
every row failed). A single table whose rows all fail while others commit (a column
or routing bug) would otherwise bleed silently, so `QueueWriter._track_table_failures`
tracks a per-table failure streak and logs a loud ERROR after
`TABLE_FAILURE_WARN_STREAK` (5) consecutive all-fail flushes for that table — the loss
then surfaces in the log and, via `last_error`, in the health snapshot.

## SQLite Schema Principles
- UUID primary keys (TEXT in SQLite)
- ISO8601 timestamps as TEXT
- Units in column names: coolant_temp_c, battery_v, speed_kmh
- WAL mode enabled: `PRAGMA journal_mode=WAL` — check return value, warn if not "wal"
- `PRAGMA synchronous=NORMAL` + `PRAGMA cache_size=-8000` on every open
- `PRAGMA integrity_check` at collector boot — rename .corrupt and start fresh on failure. Gated by `get_connection(verify_integrity=...)`: the sync process passes `False` so it doesn't full-scan the DB every 5 min (and can't quarantine the collector's live DB)
- `synced INTEGER DEFAULT 0` on every table
- The six high-volume tables carry a **partial** index `idx_<table>_unsynced ON <table>(timestamp) WHERE synced=0` (not a full `(synced)` index — that 2-value column is near-useless). It stays tiny and serves both the sync `WHERE synced=0` SELECT and the LED `MIN(timestamp) WHERE synced=0` backlog check as index-only lookups
- NULL stored for bad/missing PID responses — never carry forward

## Persistent Files on USB Drive
| File | Written by | Read by | Purpose |
|------|-----------|---------|---------|
| `/mnt/usb/data/obd.db` | collector | sync | primary data store |
| `/mnt/usb/data/restart_count` | main.py on boot | sync (health snapshot) | collector restart counter |
| `/mnt/usb/data/reconnect_count` | obd_connection.reconnect() | sync (health snapshot) | BT reconnect counter |
| `/mnt/usb/logs/obd.log` | logger | health.py (tail) | rotating log (5MB × 7) |

Both counter files are written **atomically** — `health._atomic_write()` writes a
`<path>.<pid>.tmp` file, `fsync`s it, then `os.replace()`s it onto the target and
`fsync`s the directory. A power cut (engine off) can never leave a truncated/empty
counter that the readers would silently reset to 0; a reader always sees the
complete old or new file. (Plain `open(path,"w")` truncates before writing — that
window is the bug this avoids.) The temp name carries the **pid** so that if a
stray second collector ever runs (manual test run, or a restart that left the old
process alive), the two writers don't share one `.tmp` and race — one renaming it
away makes the other's `os.replace()` fail with ENOENT. The temp is also unlinked
if the write/rename fails partway, so a failed write leaves no stray file.

## Sync — once per drive (boot-triggered), proactive-connect, then disconnect
- **One sync per drive, not a 5-min timer.** The Pi powers on with the car (one boot == one drive). `obd-sync.service` (`Type=simple`) starts `sync.py` at boot, which **loops**: poll `_check_network()` every `SYNC_POLL_S` (default 15s); when connected, drain the whole backlog; on the **first fully-successful drain** `run()` returns (exit 0). If the hotspot never appears, the loop just keeps polling cheaply until the car powers the Pi off.
- **Proactive connect = NetworkManager autoconnect (its default, left ON).** The Pi associates with the hotspot whenever it appears at any point in the drive — not just a boot window. `install.sh` can enforce `autoconnect yes` on the `HOTSPOT_CONN` profile.
- **Grace window before disconnect** = after the first successful drain, `run()` holds the link up for `SYNC_GRACE_S` (default 300s), does a **final drain** (catches data generated during the window), then exits. This both narrows the one-drive lag and gives a predictable window to SSH in for debugging (once the link drops the Pi is fully offline — you can't `nmcli` it back remotely).
- **Stop after the grace** = the unit's `ExecStopPost` runs `nmcli device disconnect wlan0` **only if `/run/obd-keep-online` is absent**. `nmcli device disconnect` drops the link **and suppresses autoconnect until the next reboot**, so the Pi stays off for the rest of the drive (no flapping) and reconnects proactively next boot.
- **Debug without being dropped:** SSH in during the grace window, then `jarvis net hold` (creates `/run/obd-keep-online`) → the post-grace disconnect is skipped, link stays up open-ended. `jarvis net release` (or reboot — `/run` is tmpfs) resumes normal behavior. **Consequence of normal operation:** a drive's data lands ~at the *start of the next drive* (one-drive lag minus the grace tail).
- Two-step network check: wlan0 has IP (hotspot) → ping Tailscale IP (server reachable). The per-poll "no hotspot / unreachable" lines are **DEBUG** (would otherwise spam the journal every `SYNC_POLL_S`).
- POSTs unsynced rows (`WHERE synced=0`) to Golang API in priority order: trips first (FK parent), obd_1s next; pi_health_log last
- Auth: Bearer token in Authorization header. Config: `/etc/obd-collector/config.env` (never committed)
- `_sync_table` returns `(rows_synced, ok)`; `ok` is False when a table didn't fully drain (POST failed, rows couldn't be marked synced, or batch cap hit). `_sync_pass` ANDs `ok` across tables; a failed pass is retried after `SYNC_POLL_S`, so a clean full drain is required before the Pi disconnects.
- Marks `synced=1` after successful POST — retries with backoff on `OperationalError`; if the local `synced=1` UPDATE never lands (writer holds the lock), the table loop **breaks** (returns ok=False) rather than re-POSTing the same `synced=0` batch (server `ON CONFLICT DO NOTHING` makes the retry safe)
- Max 1000 batches per table per pass to guard against stuck-loop (hitting it sets ok=False → retried next pass)
- Opens the DB with `get_connection(verify_integrity=False)` per pass — the per-open full `integrity_check` is skipped in the sync process; the collector owns integrity management at boot, and sync must not quarantine the live DB
- Pi health snapshot written + synced once per pass (reads reconnect_count from file)
- `cpu_usage_pct` uses `psutil.cpu_percent(interval=0.2)` — a short **blocking** sample. `interval=None` is wrong here: each pass is effectively a fresh sampling point, so "usage since last call" would return 0.0. Same trap in any short-lived/infrequent sampler.
- `config.env` is created `chmod 600` by `install.sh` (it holds the API bearer token); `.env` values may be quoted — `config.py` strips one layer of matching surrounding quotes
- Ford tables skipped silently at DEBUG level if they don't exist yet

## Status LEDs (`led_status.py`)
Two KY-016 common-cathode RGB LEDs (onboard 1kΩ resistors → drive **active-high**, no external parts) on the GPIO header, avoiding GPIO2/3 (DS3231 I²C):

| | R | G | B | GND |
|---|---|---|---|---|
| **LED A — Pipeline** | BCM17 | BCM27 | BCM22 | — |
| **LED B — Attention** | BCM5 | BCM6 | BCM13 | — |

Separate long-lived process (own systemd service), fully decoupled from the collector — it **only reads**: `systemctl is-active`, sysfs (`health.py` helpers), and **read-only** SELECTs against the DB. DB handle is opened fresh per poll with `PRAGMA query_only=ON` and closed immediately so it never pins the WAL or blocks the collector's checkpoint. Skips the DB entirely if USB is unmounted (and checks the file exists first, so `sqlite3.connect` never creates a stray DB on the SD card).

`evaluate_state(Signals) -> (Display, Display)` is a **pure function** — all colour/priority logic is unit-tested without GPIO. `gpiozero` is imported inside `LedDriver` only; `LedDriver.apply()` tracks the shown `Display` per LED and skips no-op writes so the slow blink isn't restarted every poll.

**LED A — Pipeline** (*is data being recorded?*) — priority `off > red > amber > green > blue`:
```
off    collector stopped/failed (systemctl inactive or failed)
blue   parked/connecting — up (active or activating) but obd_1s stale, no fault
green  OBD flowing — newest obd_1s within LED_DATA_STALE_S
red    FAULT — BT dongle (hci0) missing · USB unmounted · open trip but obd_1s stale
amber  Pi warning, still capturing — CPU ≥ LED_CPU_WARN_C · disk < LED_DISK_WARN_MB · rtc not ok
```
**LED B — Attention** (*does it need me?*) — dark when fine, priority `magenta > blue > green > off`:
```
off       synced, no trip, no faults
green*     open trip (end_time IS NULL)                       (* = slow blink)
blue       sync behind — oldest unsynced OBD row older than LED_SYNC_BEHIND_DAYS (default 10d)
magenta    DTC within LED_DTC_RECENT_DAYS
```
**Two-rate polling**: the loop runs every `LED_POLL_S` (2s) but only the cheap, fast-changing signals (data freshness → green, active trip → green-blink) are read each tick. The expensive slow-changing signals — `_collector_active()` (forks `systemctl`) and `_is_sync_behind()` (six-table backlog scan) — are refreshed only every `LED_SLOW_POLL_S` (30s) and cached in a `SlowSignals` between fast polls (`read_signals(slow=...)`). This keeps the background daemon off the CPU without losing drive-time responsiveness.

**DB read errors** are caught as `sqlite3.Error` (not just `OperationalError`) so a `DatabaseError` from corruption doesn't propagate and freeze the LEDs at their last colour — the fault must still be able to turn LED A red. Freshness/DTC recency checks require a **non-negative** age (`0 <= age <= window`) so a future-dated timestamp (RTC skew before NTP) can't latch a state on.

All thresholds/pins are in `config.env` (see `config.py` docstring). Verify wiring with `jarvis led test` (stops the daemon, cycles every state, restarts it).

## Logging
- **Collector** writes to `/mnt/usb/logs/obd.log` (5MB × 7 rotating files = 35MB cap).
  The rotating file handler is attached only by `main.py` via `logger.init_file_logging()`.
  Falls back to stderr-only if USB not mounted — boot WARNING always appears in journald.
- **Sync** (separate process) logs to stderr→journald only via `logger.configure_sync_logging()`
  (`journalctl -u obd-sync`). It must NOT attach the file handler — `RotatingFileHandler`
  is not multi-process safe, so collector + sync sharing one file corrupts rotation.
- Timestamps are **UTC** (`_FORMATTER.converter = time.gmtime`, `…Z` suffix) to match the
  ISO8601 UTC timestamps in SQLite.

Log these events:
- Pi boot (WARNING level so it always appears in journald)
- OBD connection established / reconnected
- First PID read success
- Trip start / trip end
- BT reconnect (with total reconnect count)
- Sync success (with row count), skipped (no hotspot), failed (with error)
- SQLite write errors
- Script restart (from systemd)
- DS3231 OSF flag set on boot

Do NOT log: individual PID values, every poll cycle, WAL checkpoints, per-tick watchdog pings.

## systemd Services
- `obd-collector.service` — Type=notify, Restart=always, RestartSec=15, WatchdogSec=60
  - `main.py` sends `READY=1` after local init (DB + queue) and does **not** block on the OBD link — `collector.start()` opens the async connection but its monitor thread reconnects in the background, so the unit reaches `active` within seconds even when parked. Blocking on the dongle before `READY=1` (the old behaviour) kept the unit `activating` until `TimeoutStartSec` while parked, then systemd killed+restarted it every ~5 min and the killed processes piled up and contended for `/dev/rfcomm0` — which then stopped any of them from connecting at all.
  - `TimeoutStartSec=300` — safety margin for the worst-case boot before `READY=1`: USB mount wait (≤120s) + the bounded initial connect attempt
  - `TimeoutStopSec=60` — covers 30s OBD timeout + 15s queue drain before SIGKILL
  - `StartLimitBurst`/`StartLimitIntervalSec` live in **`[Unit]`**, not `[Service]` — systemd ≥v230 silently ignores them in `[Service]` (defeating the crash-loop guard)
- `obd-sync.service` — Type=simple, **boot-triggered** (`WantedBy=multi-user.target`, `After=NetworkManager.service`), no `Restart=` → exactly one sync attempt-loop per boot/drive. `ExecStopPost=+/bin/sh -c '[ -f /run/obd-keep-online ] || nmcli device disconnect wlan0'` drops the link (and suppresses autoconnect till reboot) when `sync.py` exits — **unless the `jarvis net hold` flag is set** (debugging). **There is no `obd-sync.timer`** — it was removed; `install.sh` deletes any stale copy and enables the service directly.
- `obd-led.service` — Type=simple, Restart=always, RestartSec=10, `SupplementaryGroups=gpio`; runs `led_status.py`

**Hardening** (`obd-collector` + `obd-led`): `NoNewPrivileges`, `PrivateTmp`,
`ProtectSystem=strict`, `ProtectHome=read-only`, `ReadWritePaths=/mnt/usb`,
`ProtectControlGroups`, `ProtectKernelTunables`, `RestrictSUIDSGID`.
**`ReadWritePaths=/mnt/usb` is mandatory, not optional** — SQLite WAL needs
read-write access to the DB and its `-wal`/`-shm` sidecars, and a WAL *reader*
(the LED daemon) must open `-shm` read-write to map shared memory. Dropping it
breaks DB access on both units. GPIO (`/dev/gpiochip0`) and BT (`/dev/rfcomm0`)
are under `/dev`, which `ProtectSystem` does not cover, so they keep working.

## Shutdown Sequence (main.py finally block)
```
collector.stop()      → stops obd.Async loop + monitor thread
trip_manager.stop()   → stops the watchdog thread, then joins in-flight DTC scan threads (5s timeout)
queue_writer.stop()   → drains queue + commits remaining rows (15s timeout)
obd_connection.disconnect()
conn.close()
```
Order matters — DTC threads must finish before disconnect; queue must drain before close.

## Mode 22 Ford PIDs
All addresses and formulas confirmed via pid_log_20260605_190444.txt (50-run drive session).
Frame layout: `[len, 0x62, PID_H, PID_L, data_A, data_B, ...]` — data starts at index 4.

### PCM (Engine — header 7E0)
| PID | Column | Formula | Notes |
|-----|--------|---------|-------|
| 220415 | oil_pressure_kpa | `(A*256)+B` | 294–402 kPa observed at normal operating temp |
| 2203EC | knock_retard_deg | `s8(A)/2 + B/512` | Signed; mostly 0.0°, occasional -0.5 to -1.0° at light load |
| 220461 | boost_desired_psi | `((A*256)+B) * 0.0145` | 0.0 psi at light city driving; verify under WOT |
| 220462 | boost_actual_psi | `((A*256)+B) * 0.0145` | Same scale as desired; gap = boost leak or turbo wear |
| 2203CA | cac_temp_c | `s8(A)` (1 byte) | Charge air cooler temp; 84–88°C observed |
| 2203E3 | wastegate_pct | `((A*256)+B) / 100` | 15–21% at light driving |
| 220303 | vct_intake_deg | `s16(d[6],d[7]) / 16` | 4-byte response: d[4:6]=unknown ref, d[6:8]=actual position. Confirmed 0.0° vs FORScan VCT_INT_ACT1≈0.19° at warm idle 2026-06-06 |
| 220304 | vct_exhaust_deg | `(u16 - 29287) / 256` | BASE=29287 confirmed vs FORScan VCT_EXH_ACT1=0.00° at warm idle 2026-06-06. Negative = cam retarding from base for internal EGR |

### TCM (Transmission — header 7E1)
| PID | Column | Formula | Notes |
|-----|--------|---------|-------|
| 221E1C | trans_temp_c | `s16(A,B) / 16` | 79–85°C; sump temp, warms during driving |
| 221E1D | trans_oil_temp2_c | `s16(A,B) / 16` | Cooler return-line temp. Starts ~80°C (above sump), drops to ~69°C during 20-min city drive while sump rises to 85°C. Confirmed 2026-06-07. |
| 221E1A | trans_line_pressure_kpa | `(A*256)+B` | Range 299 (park/cruise) → 804 (hard accel). Heavy/idle ratio 2.68 matches Ford 8F35 spec (~2.7–2.9×). Unit assumed kPa — calibrate against known condition if Grafana threshold needed. |
| 221E12 | trans_gear | `A if 1≤A≤8 else NULL` | Values 1–6 confirmed during driving. 0x46 = Park state code → stored as NULL |
| 221E1F | tcc_ratio | `A/255 if A≠0x46 else NULL` | 0.0=unlocked, 1.0=locked. 0x46 in Park = state code → NULL |

### TCM signals observed but not collected
| PID | Range observed | Notes |
|-----|----------------|-------|
| 221E0A | 121 (park) → 514 (hard accel) | Inversely correlated with 221E11; suspected TCC apply/release hydraulic pair |
| 221E11 | 760 (park) → 235 (hard accel) | Sum with 221E0A ≈ 820–880 |
| 221E23 | 100–103 (very stable) | Low diagnostic value; likely commanded base pressure reference |

### Mode 06 Misfire Accumulators (PCM header 7E0)
Confirmed 2026-06-06. Multi-frame response (37 bytes, 4 CAN frames). First-frame layout:
`[0x10, 0x25, 0x46, TID, 0x0B, 0x24, count_hi, count_lo, ...]`
OBDMID 0x0B = misfire accumulator, SDTID 0x24 = unsigned count × 1. Value at data[6]/data[7].

| TID | Column | Formula | Notes |
|-----|--------|---------|-------|
| 06A2 | misfire_acc_cyl1 | `(data[6]<<8)\|data[7]` | cyl1 cumulative misfires, range 0–65535 |
| 06A3 | misfire_acc_cyl2 | `(data[6]<<8)\|data[7]` | cyl2 |
| 06A4 | misfire_acc_cyl3 | `(data[6]<<8)\|data[7]` | cyl3 |
| 06A5 | misfire_acc_cyl4 | `(data[6]<<8)\|data[7]` | cyl4 |

### Not yet found / resolved
| Target | Resolution |
|--------|-----------|
| fuel_rail_kpa | Resolved — standard PID 0x23 (`FUEL_RAIL_PRESSURE_DIRECT`) responds; added to obd_5s |
| oil_temp_c (Mode 22) | Not needed — Mode 01 OIL_TEMP (0x5C) works |
