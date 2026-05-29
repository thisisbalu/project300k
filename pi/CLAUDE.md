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

## OBD Connection
Always use:
```python
obd.OBD("/dev/rfcomm0", fast=False, timeout=30)
```
Known Pi Bluetooth fix — fast=False required.

## Polling Tiers
| Table | Interval | PIDs |
|-------|----------|------|
| obd_1s | 1s | rpm, speed, throttle, load |
| obd_5s | 5s | coolant_temp_c, oil_temp_c, maf_gs, stft_pct, ltft_pct, o2_b1s1_v, o2_b1s2_v |
| obd_30s | 30s | battery_v, fuel_level_pct |
| ford_obd_5s | 5s (TBC) | trans_temp_c, trans_gear, tcc_ratio |
| ford_obd_10s | 10s (TBC) | knock_retard_deg, boost_desired_psi, boost_actual_psi, wastegate_pct |
| ford_obd_20s | 20s (TBC) | misfire_cyl1–4, fuel_rail_pressure_psi |

Ford table cadences to be confirmed after FORScan baseline scan.

## Trip Detection
- Trip start: battery_v > 13.0V AND rpm > 0
- Trip end: rpm = 0 for >30s AND battery_v < 12.5V
- Polling pause: pause obd_1s + obd_5s when rpm=0 for >30s; obd_30s keeps running

## SQLite Schema Principles
- UUID primary keys (TEXT in SQLite)
- ISO8601 timestamps as TEXT
- Units in column names: coolant_temp_c, battery_v, speed_kmh
- WAL mode enabled: PRAGMA journal_mode=WAL
- synced INTEGER DEFAULT 0 on every table
- NULL stored for bad/missing PID responses — never carry forward

## Sync
- Fires 5 min after boot via systemd timer
- POSTs unsynced rows (WHERE synced=0) to Golang API
- API endpoint: http://<tailscale-ip>:8080/sync
- Auth: Bearer token in Authorization header
- Config: /etc/obd-collector/config.env (never committed to git)
- Marks rows synced=1 after successful POST
- On failure: skip, retry next boot
- Pi health snapshot included in every sync payload

## Logging
Log file: /mnt/usb/logs/obd.log (5MB × 7 rotating files)

Log these events:
- Pi boot
- OBD connection established (with device name)
- First PID read success
- Trip start / trip end
- BT connect / disconnect
- Sync success (with row count)
- Sync skipped — no hotspot
- Sync failed (with error)
- SQLite write errors
- Watchdog ping sent
- Script restart (from systemd)
- DS3231 OSF flag set on boot (WARNING: RTC battery may be dead)

Do NOT log: individual PID values, every poll cycle, WAL checkpoints.

## systemd Services
- obd-collector.service — Restart=always, RestartSec=15, StartLimitBurst=5
- obd-sync.service — runs sync script
- obd-sync.timer — triggers sync 5 min after boot

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
