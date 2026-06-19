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
                          iPhone hotspot   │  (WiFi, once per drive)
                          auto-enabled via │  batch sync over Tailscale,
                          iOS Shortcuts    │  then disconnect
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

37 PIDs across six tables, polled on tiered intervals via a single async OBD
connection. Standard PIDs come from OBD-II Mode 01; Ford-enhanced PIDs come from
Mode 22 (PCM/TCM) and Mode 06 (misfire accumulators), confirmed on this specific
2.0L EcoBoost by live-car validation. ~192,000 rows/month. Storage runs until
300,000 km, not a fixed time horizon.

Every value is stored with its unit in the column name and `NULL` on a bad read
(never carried forward). Each row is one combined reading per tier, not one row
per PID. A full DTC scan (stored + pending codes) runs at every trip start and end.

### Standard OBD-II — `obd_1s` (every 1s)

Core engine state, sampled densely because it changes fast and anchors every
other signal in time.

| Column | What it is | Why it matters long-term |
|--------|-----------|--------------------------|
| `rpm` | Engine speed | Idle RPM creeping up over months signals vacuum leak or throttle-body fouling; the baseline every other reading is correlated against. |
| `speed_kmh` | Vehicle speed | Integrated into trip distance (odometer proxy) and drive-cycle context for load analysis. |
| `throttle_pct` | Throttle position | Driver-demand context — separates "engine working hard" from "engine struggling" when paired with load and boost. |
| `load_pct` | Calculated engine load | Distinguishes genuine engine wear from driving style; normalises fuel and thermal trends across different drives. |

### Standard OBD-II — `obd_5s` (every 5s)

Thermals, air, and fuel — the slower-moving health signals.

| Column | What it is | Why it matters long-term |
|--------|-----------|--------------------------|
| `coolant_temp_c` | Engine coolant temp | Thermostat and cooling-system health; a slow upward overheating trend is an early head-gasket / water-pump warning. |
| `oil_temp_c` | Engine oil temp | *Not supported via Mode 01 on this engine — stored NULL.* Kept for portability. |
| `intake_air_temp_c` | Intake air temp | Sensor sanity check; correlates with charge-air-cooler temp and ambient to spot intake heat problems. |
| `maf_gs` | Mass air flow | *Not fitted / unsupported — this engine is speed-density (MAP-based). Stored NULL.* |
| `map_kpa` | Manifold absolute pressure | Engine breathing and vacuum health; the primary airflow signal on this MAP-based engine. |
| `baro_pressure_kpa` | Barometric pressure | Altitude/weather reference that normalises boost and load math. |
| `stft_pct` | Short-term fuel trim | Live mixture correction — transient spikes flag momentary fueling faults. |
| `ltft_pct` | Long-term fuel trim | **A top fueling-health signal.** Slow drift reveals vacuum leaks, injector wear, fuel-pump decline, or sensor drift long before a fault code. (Currently sitting ~+6–7%, worth watching.) |
| `o2_b1s1_v` | Upstream O2 sensor voltage | Sensor aging (sluggish switching) and closed-loop fueling health. |
| `o2_b1s2_v` | Downstream O2 voltage | *Not accessible via Mode 01 on this car — stored NULL.* Intended for catalytic-converter monitoring. |
| `timing_advance_deg` | Ignition timing advance | Paired with knock retard — timing being pulled points to fuel quality or carbon buildup. |
| `fuel_rail_kpa` | GDI fuel-rail pressure | High-pressure fuel-pump wear (a known GDI weak point) and injector health. |

### Standard OBD-II — `obd_30s` (every 30s)

Slow-moving signals that barely change minute to minute.

| Column | What it is | Why it matters long-term |
|--------|-----------|--------------------------|
| `battery_v` | Control-module voltage | Alternator and battery health — declining charge voltage means a tiring alternator; resting voltage tracks battery aging. Critical alert outside 12.0–15.0V. |
| `fuel_level_pct` | Fuel tank level | Per-trip fuel-economy calculation and range tracking. |
| `ambient_air_temp_c` | Outside air temp | Environmental context for thermal trends — cold-start behavior in winter, heat-soak in summer. |
| `distance_since_dtc_cleared_km` | Distance since DTCs cleared | Drive-cycle readiness and context for how quickly a fault recurs after clearing. |

### Ford Mode 22 — TCM, `ford_obd_5s` (every 5s)

Transmission internals, read from the transmission control module (8F35 gearbox).

| Column | What it is | Why it matters long-term |
|--------|-----------|--------------------------|
| `trans_temp_c` | Sump fluid temp | **The single most important transmission longevity signal** — sustained heat is the number-one killer of automatics and drives fluid-change timing. |
| `trans_oil_temp2_c` | Cooler return-line temp | Transmission-cooler efficiency: the gap between this and sump temp widening over time means a clogged cooler or degraded fluid. |
| `trans_line_pressure_kpa` | Hydraulic line pressure | Pump and valve-body/solenoid health — pressure falling for a given load is a direct sign of internal wear. |
| `trans_gear` | Current gear (NULL in Park) | Shift-pattern analysis and gear-hunting detection; lets temp and pressure be analysed per gear. |
| `tcc_ratio` | Torque-converter clutch lockup (NULL in Park) | TCC slip detection — a slipping lockup clutch generates heat and is an early wear warning. |

### Ford Mode 22 — PCM, `ford_obd_10s` (every 10s)

Boost, combustion, cam timing, and oil — read from the powertrain control module.

| Column | What it is | Why it matters long-term |
|--------|-----------|--------------------------|
| `oil_pressure_kpa` | Engine oil pressure | **A top engine-longevity signal** — low pressure at hot idle points to bearing wear or a tiring oil pump. |
| `knock_retard_deg` | Timing pulled by the knock sensor | Carbon buildup on GDI intake valves, fuel quality, or pre-ignition; sustained knock risks internal damage. |
| `boost_desired_psi` | Commanded boost | Baseline for turbo health — meaningful only next to actual boost. |
| `boost_actual_psi` | Measured boost | Turbo wear and boost-leak detection: a widening gap from desired means a degrading turbo, wastegate, or intercooler. *(Reads ~0 in city driving — needs a wide-open-throttle pull to fully validate.)* |
| `cac_temp_c` | Charge-air-cooler temp | Intercooler efficiency and heat-soak under sustained load. |
| `wastegate_pct` | Wastegate duty cycle | Boost-control-system and actuator wear. |
| `vct_intake_deg` | Variable cam timing, intake | VCT phaser/solenoid wear (a common Ford failure mode), often oil-condition related. *(Reads ~0 at light throttle — validate under load.)* |
| `vct_exhaust_deg` | Variable cam timing, exhaust | Same as above for the exhaust cam; retards for internal EGR, so its range also reflects emissions-control health. |

### Ford Mode 06 — PCM, `ford_obd_20s` (every 20s)

Per-cylinder misfire accumulators — cumulative lifetime counters, ideal for
longitudinal tracking to 300K km.

| Column | What it is | Why it matters long-term |
|--------|-----------|--------------------------|
| `misfire_acc_cyl1` … `cyl4` | Cumulative misfire count, each cylinder | **The earliest ignition/combustion warning.** Watching the trend per cylinder isolates a failing coil, plug, or injector to one cylinder — well before a misfire DTC ever sets. |

### DTC scans (trip start + end)

Full Mode 03 (stored) **and** Mode 07 (pending) codes on every trip boundary.
Pending codes are the earliest warning of all — they appear before the check-
engine light, and a code that shows up pending on consecutive trips is the
system's cue to flag a developing fault.

---

## Hardware

### In Car (~$216 CAD)
| Item | Cost |
|------|------|
| OBDLink MX+ | ~$120 |
| OBD extension cable with switch | ~$12 |
| Raspberry Pi 3B (Kano kit — already owned) | — |
| TP-Link UB500 USB Bluetooth dongle | ~$15 |
| SanDisk Ultra Fit 32GB USB flash drive | ~$12 |
| Samsung Pro Endurance 32GB microSD | ~$15 |
| Two KY-016 RGB LED modules (status indicators) | ~$4 |
| Pi case | ~$8 |
| micro USB OTG adapter | ~$5 |

Two RGB status LEDs wired to the GPIO header report system health at a glance,
driven by a decoupled `obd-led` daemon. **LED A (Pipeline)** — off = collector
stopped, blue = up/connecting, green = data flowing, red = fault, amber = Pi
warning. **LED B (Attention)** — dark when healthy, green-blink = trip active,
blue = sync behind, magenta = recent DTC. See `pi/CLAUDE.md` for the full spec.

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

1. **Part 1 — In-car Pi system** ✅ *complete, live in car*
   - OBDLink MX+ → Pi → SQLite → sync to server via iPhone hotspot
   - systemd services, hardware watchdog, four-layer monitoring
   - Sync runs **once per drive** (boot-triggered): proactive hotspot connect →
     drain backlog over Tailscale → 5-min grace → disconnect

2. **Part 2 — Home server** 🚧 *foundation built*
   - **Done:** PostgreSQL schema + migrations, Golang `/sync` API (stdlib + pgx),
     Docker stack (Tailscale sidecar + Postgres + API). Real Pi data is syncing in.
   - **Pending:** Grafana dashboards, Claude API analysis, ntfy/email alerts,
     `pg_dump`→rclone backups, `distance_km` computation.
   - **Note:** running on a **laptop as a temporary server** (~2 months) until the
     mini-PC is bought; the Pi reaches it over the private tailnet only.

3. **Part 3 — Web dashboard** *(deferred)*
   - Trip history, health trends, DTC log, Claude analysis UI

---

## Why

This car is the daily driver for the next decade. Mechanical problems caught at 50K km cost a fraction of what they cost at 200K km. The goal is a complete health record from day one — not a black box that fails silently.
