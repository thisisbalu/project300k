# Project 300K

Make a 2023 Ford Bronco Sport Badlands comfortably run beyond 300,000 km.

Not passive monitoring — active longevity engineering. The system catches problems early, drives maintenance decisions, and builds a 10-year health record of the vehicle.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         IN CAR                                  │
│                                                                 │
│   OBD Port                                                      │
│      │                                                          │
│   OBDLink MX+  ──(Bluetooth Classic)──►  Raspberry Pi 3B       │
│   (dongle)                               │                      │
│                                          │  USB BT dongle       │
│                                          │  (BT Classic only)   │
│                                          │                      │
│                                          │  SQLite on USB drive │
│                                          │  WAL mode, synced=0  │
│                                          │                      │
│                                          │  systemd services    │
│                                          │  hardware watchdog   │
└──────────────────────────────────────────┼──────────────────────┘
                                           │
                          iPhone hotspot   │  (WiFi, 5 min after boot)
                          auto-enabled via │  batch sync unsynced rows
                          iOS Shortcuts    │
                                           │
┌──────────────────────────────────────────▼──────────────────────┐
│                       HOME SERVER                               │
│                    (Dell OptiPlex / HP ProDesk Mini)            │
│                    Ubuntu 24.04 LTS                             │
│                                                                 │
│   Golang API  ◄──── POST /sync (OBD rows + Pi health)          │
│       │                                                         │
│       ▼                                                         │
│   PostgreSQL                                                    │
│   ├── obd_readings                                              │
│   ├── trips                                                     │
│   ├── dtc_events                                                │
│   └── pi_health_log                                             │
│       │                                                         │
│       ├──► Grafana dashboards + staleness alerts                │
│       └──► Claude API analysis (on demand)                      │
│                                                                 │
│   Tailscale (remote SSH)                                        │
│   Cloudflare Tunnel (public dashboard URL)                      │
└─────────────────────────────────────────────────────────────────┘
```

---

## Data Collection

| Interval | PIDs |
|----------|------|
| Every 1s | RPM, speed, throttle position, engine load |
| Every 5s | Coolant temp, oil temp, MAF, short/long fuel trims, O2 sensors |
| Every 30s | Battery voltage, fuel level |
| Trip start/end | Full DTC scan |

~44,000 rows/month (~17MB). Projected 10-year total: ~5M rows (~2GB).

---

## Hardware

### In Car (~$212 CAD)
| Item | Cost |
|------|------|
| OBDLink MX+ | ~$120 |
| OBD extension cable with switch | ~$12 |
| Raspberry Pi 3B (Kano kit — already owned) | — |
| TP-Link UB500 USB Bluetooth dongle | ~$15 |
| SanDisk Ultra Fit 32GB USB flash drive | ~$12 |
| Samsung Pro Endurance 32GB microSD | ~$15 |
| Pi case | ~$8 |
| micro USB OTG adapter | ~$5 |

### Home Server (~$350 CAD)
Dell OptiPlex 7080 Micro or HP ProDesk 600 G6 Mini — 16GB+ RAM, 256GB+ NVMe, i7 10th gen.

---

## Software Stack

| Layer | Tech |
|-------|------|
| OBD collection | Python + python-obd (async mode) |
| Local storage | SQLite (WAL mode, USB flash drive) |
| Server database | PostgreSQL |
| API | Golang |
| Dashboards | Grafana |
| AI analysis | Claude API (~$0.50 CAD/month) |
| Remote access | Tailscale |
| Public URL | Cloudflare Tunnel |

---

## Claude API Analysis

PostgreSQL pre-aggregates trip data → compact JSON (~3K–5K tokens) sent on demand.

Checks: DTCs, coolant temp trends, fuel trim drift, O2 sensor health, battery voltage, idle RPM drift, fuel efficiency.

Returns: Red / Yellow / Green flags + plain English diagnosis.

---

## Repository Layout

```
project300k/
├── pi/          Part 1 — Python OBD collector (Raspberry Pi)
├── backend/     Part 2 — Golang API + PostgreSQL
├── frontend/    Part 3 — Web dashboard
└── README.md
```

---

## Build Order

1. **Part 1 — In-car Pi system**
   - OBDLink MX+ → Pi → SQLite → sync to server via iPhone hotspot
   - systemd services, hardware watchdog, four-layer monitoring

2. **Part 2 — Home server**
   - PostgreSQL schema, Golang sync API, Grafana dashboards, Claude API analysis

3. **Part 3 — Web dashboard**
   - Trip history, health trends, DTC log, Claude analysis UI

---

## Why

This car is the daily driver for the next decade. Mechanical problems caught at 50K km cost a fraction of what they cost at 200K km. The goal is a complete health record from day one — not a black box that fails silently.
