"""
obd_commands.py — OBD PID definitions for the Bronco Sport 2.0L EcoBoost.

Defines all PIDs the collector polls, organised into polling tier groups.
Each PID is wrapped in a PIDConfig that carries the OBD command, the target
SQLite table, the column name, and the polling interval. This lets collector.py
register all watchers with a single loop rather than hardcoding each one.

Standard Mode 01 PIDs (SAE J1979) are fully defined here and ready to use.
Ford Mode 22 enhanced PIDs are stubbed — populated after FORScan baseline
scan confirms the hex addresses on this specific VIN.

Polling tiers:
    STANDARD_1S   — 1s:  core engine state (RPM, speed, throttle, load)
    STANDARD_5S   — 5s:  thermals, air/fuel, O2 sensors
    STANDARD_30S  — 30s: battery voltage, fuel level

    FORD_5S       — 5s (TBC):  transmission data (Mode 22, TCM module)
    FORD_10S      — 10s (TBC): boost and knock (Mode 22, PCM module)
    FORD_20S      — 20s (TBC): misfire counters, fuel rail (Mode 22 + Mode 06)

ALL_PIDS is a flat list of every active PIDConfig, consumed by collector.py.

References:
    Standard PIDs: SAE J1979 Mode 01
    Ford Mode 22:  Ford PCM/TCM proprietary, ISO 15765-4 CAN
    Mode 06:       SAE J1979 Mode 06, standardised misfire counters on CAN
"""

import obd
from dataclasses import dataclass


@dataclass(frozen=True)
class PIDConfig:
    """Binding between an OBD command and its SQLite destination.

    Consumed by collector.py to register async watchers and route
    callback values to the correct table and column.

    Attributes:
        command:     python-obd OBDCommand to watch.
        table:       Target SQLite table (e.g. "obd_1s").
        column:      Target column name (e.g. "rpm"). Must match the schema.
        interval_s:  Polling interval in seconds.
    """
    command: obd.OBDCommand
    table: str
    column: str
    interval_s: int


# ---------------------------------------------------------------------------
# Standard OBD-II Mode 01 — SAE J1979, supported by every OBD-II vehicle.
# All commands are built into python-obd 0.7.3 — no custom definitions needed.
# ---------------------------------------------------------------------------

STANDARD_1S: list[PIDConfig] = [
    PIDConfig(
        command=obd.commands.RPM,
        table="obd_1s",
        column="rpm",
        interval_s=1,
        # Core health signal — idle RPM drift over thousands of km indicates
        # engine wear (valves, rings). High-frequency capture is essential.
    ),
    PIDConfig(
        command=obd.commands.SPEED,
        table="obd_1s",
        column="speed_kmh",
        interval_s=1,
    ),
    PIDConfig(
        command=obd.commands.THROTTLE_POS,
        table="obd_1s",
        column="throttle_pct",
        interval_s=1,
    ),
    PIDConfig(
        command=obd.commands.ENGINE_LOAD,
        table="obd_1s",
        column="load_pct",
        interval_s=1,
    ),
]

STANDARD_5S: list[PIDConfig] = [
    PIDConfig(
        command=obd.commands.COOLANT_TEMP,
        table="obd_5s",
        column="coolant_temp_c",
        interval_s=5,
        # Gradual upward trend over weeks = early thermostat or coolant leak sign.
        # Sudden spike = immediate stop-driving condition (>105°C alert threshold).
    ),
    PIDConfig(
        command=obd.commands.OIL_TEMP,
        table="obd_5s",
        column="oil_temp_c",
        interval_s=5,
        # PID 0x5C — standardised in OBD-II since 2008. Oil running consistently
        # above 130°C accelerates oxidation and viscosity breakdown.
    ),
    PIDConfig(
        command=obd.commands.MAF,
        table="obd_5s",
        column="maf_gs",
        interval_s=5,
    ),
    PIDConfig(
        command=obd.commands.SHORT_FUEL_TRIM_1,
        table="obd_5s",
        column="stft_pct",
        interval_s=5,
        # Real-time ECU correction to the injector pulse. Values outside ±10%
        # indicate an active fuelling problem (O2 sensor, vacuum leak, injector).
    ),
    PIDConfig(
        command=obd.commands.LONG_FUEL_TRIM_1,
        table="obd_5s",
        column="ltft_pct",
        interval_s=5,
        # Learned correction applied permanently. LTFT drifting beyond ±10%
        # and staying there is a strong diagnostic signal — alert threshold.
    ),
    PIDConfig(
        command=obd.commands.O2_B1S1,
        table="obd_5s",
        column="o2_b1s1_v",
        interval_s=5,
        # Pre-catalyst O2 sensor. Should oscillate 0.1–0.9V in closed loop.
        # Flatline = dead sensor. Stuck high/low = rich/lean condition.
    ),
    PIDConfig(
        command=obd.commands.O2_B1S2,
        table="obd_5s",
        column="o2_b1s2_v",
        interval_s=5,
        # Post-catalyst O2 sensor. Should read stable ~0.6–0.7V if catalyst
        # is healthy. Oscillating like B1S1 = catalyst efficiency loss.
    ),
    PIDConfig(
        command=obd.commands.INTAKE_TEMP,
        table="obd_5s",
        column="intake_air_temp_c",
        interval_s=5,
        # Hot intake air reduces charge density and power. Also provides
        # context for fuel trim readings — high IAT can cause lean corrections.
    ),
    PIDConfig(
        command=obd.commands.INTAKE_PRESSURE,
        table="obd_5s",
        column="map_kpa",
        interval_s=5,
        # Manifold Absolute Pressure. map_kpa - baro_pressure_kpa = calculated
        # boost pressure — useful before Ford Mode 22 boost PIDs are confirmed.
    ),
    PIDConfig(
        command=obd.commands.BAROMETRIC_PRESSURE,
        table="obd_5s",
        column="baro_pressure_kpa",
        interval_s=5,
        # Ambient barometric pressure. Required to calculate true boost from MAP.
        # Also provides altitude context for fuel trim analysis.
    ),
    PIDConfig(
        command=obd.commands.TIMING_ADVANCE,
        table="obd_5s",
        column="timing_advance_deg",
        interval_s=5,
        # Ignition timing advance in degrees. Context for knock retard readings
        # in ford_obd_10s — shows how aggressively ECU is advancing timing.
    ),
]

STANDARD_30S: list[PIDConfig] = [
    PIDConfig(
        command=obd.commands.CONTROL_MODULE_VOLTAGE,
        table="obd_30s",
        column="battery_v",
        interval_s=30,
        # Engine running: 13.8–14.4V = healthy alternator.
        # <12.0V running = alternator failing (critical alert).
        # >15.0V running = alternator overcharging (critical alert).
    ),
    PIDConfig(
        command=obd.commands.FUEL_LEVEL,
        table="obd_30s",
        column="fuel_level_pct",
        interval_s=30,
    ),
    PIDConfig(
        # Note: "AMBIANT" is a typo in the python-obd 0.7.3 library itself
        # (should be AMBIENT). Do NOT "fix" this spelling — it must match
        # the library constant exactly or the import will fail at runtime.
        command=obd.commands.AMBIANT_AIR_TEMP,
        table="obd_30s",
        column="ambient_air_temp_c",
        interval_s=30,
        # Outside air temperature. Provides context for cold start behaviour,
        # seasonal thermal trends, and correlating engine wear with temperature
        # across the 300K km dataset.
    ),
    PIDConfig(
        command=obd.commands.DISTANCE_W_MIL,
        table="obd_30s",
        column="distance_since_dtc_cleared_km",
        interval_s=30,
        # Distance travelled since DTCs were last cleared. Useful alongside
        # dtc_events to understand how long a fault has been present or how
        # long the vehicle has run cleanly since the last fault was resolved.
    ),
]


# ---------------------------------------------------------------------------
# Ford Mode 22 Enhanced PIDs — requires OBDLink MX+ (not generic ELM327).
# Stubbed until FORScan baseline scan confirms hex addresses on this VIN.
# ---------------------------------------------------------------------------

# TCM module — transmission data
# Expected: trans_temp_c (221E1C), trans_gear (221E12), tcc_ratio (221E15)
FORD_5S: list[PIDConfig] = []
# TODO: Task 7 (Part 2) — define after FORScan scan confirms TCM PID addresses

# PCM module — turbo and knock data
# Expected: knock_retard_deg (220318), boost_desired_psi (22033E),
#           boost_actual_psi (22D137), wastegate_pct (2203CA)
FORD_10S: list[PIDConfig] = []
# TODO: Task 7 (Part 2) — define after FORScan scan confirms PCM PID addresses

# PCM module + Mode 06 — misfire counters and fuel rail pressure
# Expected: misfire_cyl1-4 (Mode 06: 06A20C-06A50C),
#           fuel_rail_pressure_psi (Mode 22: address TBD from FORScan)
FORD_20S: list[PIDConfig] = []
# TODO: Task 7 (Part 2) — define after FORScan scan confirms misfire + fuel rail addresses


# ---------------------------------------------------------------------------
# ALL_PIDS — flat list of every active PIDConfig consumed by collector.py.
# Ford lists are empty until FORScan confirms addresses — adding them here
# means collector.py requires no changes when Ford PIDs are enabled.
# ---------------------------------------------------------------------------

ALL_PIDS: list[PIDConfig] = [
    *STANDARD_1S,
    *STANDARD_5S,
    *STANDARD_30S,
    *FORD_5S,
    *FORD_10S,
    *FORD_20S,
]
