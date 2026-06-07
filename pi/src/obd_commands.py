"""
obd_commands.py — OBD PID definitions for the Bronco Sport 2.0L EcoBoost.

Defines all PIDs the collector polls, organised into polling tier groups.
Each PID is wrapped in a PIDConfig that carries the OBD command, the target
SQLite table, the column name, and the polling interval. This lets collector.py
register all watchers with a single loop rather than hardcoding each one.

Standard Mode 01 PIDs (SAE J1979) are fully defined here and ready to use.
Ford Mode 22 addresses and formulas confirmed via pid_log_20260605_190444.txt.

Polling tiers:
    STANDARD_1S   — 1s:  core engine state (RPM, speed, throttle, load)
    STANDARD_5S   — 5s:  thermals, air/fuel, O2 sensors
    STANDARD_30S  — 30s: battery voltage, fuel level

    FORD_5S       — 5s:  transmission data (Mode 22, TCM — confirmed)
    FORD_10S      — 10s: boost, knock, VCT, oil pressure (Mode 22, PCM — confirmed)
    FORD_20S      — 20s: misfire accumulators per cylinder (Mode 06, confirmed)

ALL_PIDS is a flat list of every active PIDConfig, consumed by collector.py.

References:
    Standard PIDs: SAE J1979 Mode 01
    Ford Mode 22:  Ford PCM/TCM proprietary, ISO 15765-4 CAN
    Mode 06:       SAE J1979 Mode 06, standardised misfire counters on CAN
"""

from __future__ import annotations

import obd
from obd import OBDCommand, ECU
from dataclasses import dataclass


def _s8(b: int) -> int:
    return b - 256 if b > 127 else b


def _s16(hi: int, lo: int) -> int:
    v = (hi << 8) | lo
    return v - 65536 if v > 32767 else v


def _mode22(n_data: int, formula):
    """Decoder factory for Ford Mode 22 PIDs.

    Frame layout confirmed from pid_log_20260605_190444:
        [len, 0x62, PID_H, PID_L, data_A, data_B, ...]
    Data bytes start at index 4. Returns None on any error so the
    collector stores NULL rather than crashing or carrying stale values.
    """
    min_len = 4 + n_data

    def decoder(messages):
        if not messages:
            return None
        try:
            data = list(bytes(messages[0].frames[0].data))
            if len(data) < min_len or data[1] != 0x62:
                return None
            return formula(data)
        except (AttributeError, IndexError, TypeError, ZeroDivisionError):
            return None

    return decoder


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
    PIDConfig(
        command=obd.commands.FUEL_RAIL_PRESSURE_DIRECT,
        table="obd_5s",
        column="fuel_rail_kpa",
        interval_s=5,
        # GDI high-pressure fuel rail (PID 0x23). Idle ≈1400 kPa; higher under load.
        # Long-term decline at same load points = high-pressure fuel pump wear.
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
# All addresses and formulas confirmed via pid_log_20260605_190444.txt
# (50-run drive session on 2023 Bronco Sport Badlands 2.0L EcoBoost).
# ---------------------------------------------------------------------------

# --- PCM commands ---

_FORD_OIL_PRESSURE = OBDCommand(
    "oil_pressure_kpa", "Engine oil pressure",
    b"220415", 6,
    _mode22(2, lambda d: (d[4] * 256) + d[5]),
    ECU.ALL, fast=False,
)

_FORD_KNOCK_RETARD = OBDCommand(
    "knock_retard_deg", "Ignition timing retard from knock",
    b"2203EC", 6,
    _mode22(2, lambda d: round(_s8(d[4]) / 2 + d[5] / 512, 3)),
    ECU.ALL, fast=False,
)

_FORD_BOOST_DESIRED = OBDCommand(
    "boost_desired_psi", "Desired turbo boost pressure",
    b"220461", 6,
    _mode22(2, lambda d: round(((d[4] * 256) + d[5]) * 0.0145, 2)),
    ECU.ALL, fast=False,
)

_FORD_BOOST_ACTUAL = OBDCommand(
    "boost_actual_psi", "Actual turbo boost pressure",
    b"220462", 6,
    _mode22(2, lambda d: round(((d[4] * 256) + d[5]) * 0.0145, 2)),
    ECU.ALL, fast=False,
)

_FORD_CAC_TEMP = OBDCommand(
    "cac_temp_c", "Charge air cooler temperature",
    b"2203CA", 5,
    _mode22(1, lambda d: _s8(d[4])),
    ECU.ALL, fast=False,
)

_FORD_WASTEGATE = OBDCommand(
    "wastegate_pct", "Wastegate duty cycle",
    b"2203E3", 6,
    _mode22(2, lambda d: round(((d[4] * 256) + d[5]) / 100, 2)),
    ECU.ALL, fast=False,
)

_FORD_VCT_INTAKE = OBDCommand(
    "vct_intake_deg", "Variable cam timing intake position",
    b"220303", 8,
    # Response has 4 data bytes: d[4:6]=unknown reference, d[6:8]=actual cam position.
    # Confirmed against FORScan VCT_INT_ACT1 at warm idle 2026-06-06.
    _mode22(4, lambda d: round(_s16(d[6], d[7]) / 16, 2)),
    ECU.ALL, fast=False,
)

_FORD_VCT_EXHAUST = OBDCommand(
    "vct_exhaust_deg", "Variable cam timing exhaust position",
    b"220304", 6,
    # Scale is /256. BASE=29287 confirmed against FORScan VCT_EXH_ACT1=0.00°
    # at warm idle Park 2026-06-06. Values are negative at idle (cam retards
    # from base for internal EGR): idle≈0°, city driving≈-9°, highway≈-1.6°.
    _mode22(2, lambda d: round(((d[4] * 256) + d[5] - 29287) / 256, 2)),
    ECU.ALL, fast=False,
)

# --- TCM commands ---

_FORD_TRANS_TEMP = OBDCommand(
    "trans_temp_c", "Transmission fluid temperature",
    b"221E1C", 6,
    _mode22(2, lambda d: round(_s16(d[4], d[5]) / 16, 1)),
    ECU.ALL, fast=False,
)

_FORD_TRANS_GEAR = OBDCommand(
    "trans_gear", "Current transmission gear",
    b"221E12", 5,
    # Values 1–6 confirmed as actual gear. 0x46 is a Park/Neutral state code
    # returned by the TCM when no drive gear is engaged — store as NULL.
    # Values 7–8 and transitional shift bytes are also stored as NULL.
    _mode22(1, lambda d: d[4] if 1 <= d[4] <= 8 else None),
    ECU.ALL, fast=False,
)

_FORD_TCC_RATIO = OBDCommand(
    "tcc_ratio", "Torque converter clutch lockup ratio",
    b"221E1F", 5,
    # 0.0=open, 1.0=fully locked. 0x46 returned in Park (TCM state code,
    # not a ratio) — store as NULL so Grafana shows a gap, not 0.275.
    _mode22(1, lambda d: round(d[4] / 255, 3) if d[4] != 0x46 else None),
    ECU.ALL, fast=False,
)

_FORD_TRANS_LINE_PRESSURE = OBDCommand(
    "trans_line_pressure_kpa", "Transmission line pressure",
    b"221E1A", 6,
    # Range 299 (park/cruise) → 804 (hard accel). Heavy/idle ratio 2.68 matches
    # Ford 8F35 WOT/idle line pressure ratio (~2.7–2.9×). Unit assumed kPa.
    _mode22(2, lambda d: (d[4] * 256) + d[5]),
    ECU.ALL, fast=False,
)

_FORD_TRANS_TEMP2 = OBDCommand(
    "trans_oil_temp2_c", "Transmission oil temperature sensor 2",
    b"221E1D", 6,
    # Second TCM temperature sensor — same formula as trans_temp_c (221E1C).
    # Drive data 2026-06-07: starts ~80°C (above sump), falls to ~69°C during
    # 20-min city drive while sump climbs to 85°C — consistent with cooler
    # return-line position (fluid exits trans hot, returns cooler via radiator).
    _mode22(2, lambda d: round(_s16(d[4], d[5]) / 16, 1)),
    ECU.ALL, fast=False,
)


def _mode06_misfire():
    """Decoder for Mode 06 TID A2–A5 misfire accumulator (OBDMID 0x0B).

    Mode 06 responses are multi-frame (37 bytes). ELM327 passes raw first-frame
    CAN bytes: [0x10, 0x25, 0x46, TID, 0x0B, 0x24, count_hi, count_lo, ...]
    OBDMID 0x0B confirmed as misfire accumulator via 2026-06-06 live scan.
    Test value bytes at data[6]/data[7] = cumulative misfire count for cylinder.
    """
    def decoder(messages):
        if not messages:
            return None
        try:
            data = list(bytes(messages[0].frames[0].data))
            # First Frame: data[2]=0x46 (Mode 06 response), data[4]=0x0B (OBDMID)
            if len(data) < 8 or data[2] != 0x46 or data[4] != 0x0B:
                return None
            return (data[6] << 8) | data[7]
        except (AttributeError, IndexError, TypeError):
            return None
    return decoder


_MISFIRE_DECODER = _mode06_misfire()

_FORD_MISFIRE_CYL1 = OBDCommand(
    "misfire_acc_cyl1", "Misfire accumulator cylinder 1",
    b"06A2", 8, _MISFIRE_DECODER, ECU.ALL, fast=False,
)
_FORD_MISFIRE_CYL2 = OBDCommand(
    "misfire_acc_cyl2", "Misfire accumulator cylinder 2",
    b"06A3", 8, _MISFIRE_DECODER, ECU.ALL, fast=False,
)
_FORD_MISFIRE_CYL3 = OBDCommand(
    "misfire_acc_cyl3", "Misfire accumulator cylinder 3",
    b"06A4", 8, _MISFIRE_DECODER, ECU.ALL, fast=False,
)
_FORD_MISFIRE_CYL4 = OBDCommand(
    "misfire_acc_cyl4", "Misfire accumulator cylinder 4",
    b"06A5", 8, _MISFIRE_DECODER, ECU.ALL, fast=False,
)


# TCM module — transmission data. Addresses confirmed.
FORD_5S: list[PIDConfig] = [
    PIDConfig(command=_FORD_TRANS_TEMP,           table="ford_obd_5s", column="trans_temp_c",            interval_s=5),
    PIDConfig(command=_FORD_TRANS_TEMP2,          table="ford_obd_5s", column="trans_oil_temp2_c",       interval_s=5),
    PIDConfig(command=_FORD_TRANS_LINE_PRESSURE,  table="ford_obd_5s", column="trans_line_pressure_kpa", interval_s=5),
    PIDConfig(command=_FORD_TRANS_GEAR,           table="ford_obd_5s", column="trans_gear",               interval_s=5),
    PIDConfig(command=_FORD_TCC_RATIO,            table="ford_obd_5s", column="tcc_ratio",                interval_s=5),
]

# PCM module — boost, knock, VCT, oil pressure. Addresses confirmed.
FORD_10S: list[PIDConfig] = [
    PIDConfig(command=_FORD_OIL_PRESSURE,  table="ford_obd_10s", column="oil_pressure_kpa",  interval_s=10),
    PIDConfig(command=_FORD_KNOCK_RETARD,  table="ford_obd_10s", column="knock_retard_deg",   interval_s=10),
    PIDConfig(command=_FORD_BOOST_DESIRED, table="ford_obd_10s", column="boost_desired_psi",  interval_s=10),
    PIDConfig(command=_FORD_BOOST_ACTUAL,  table="ford_obd_10s", column="boost_actual_psi",   interval_s=10),
    PIDConfig(command=_FORD_CAC_TEMP,      table="ford_obd_10s", column="cac_temp_c",         interval_s=10),
    PIDConfig(command=_FORD_WASTEGATE,     table="ford_obd_10s", column="wastegate_pct",       interval_s=10),
    PIDConfig(command=_FORD_VCT_INTAKE,    table="ford_obd_10s", column="vct_intake_deg",      interval_s=10),
    PIDConfig(command=_FORD_VCT_EXHAUST,   table="ford_obd_10s", column="vct_exhaust_deg",     interval_s=10),
]

# PCM module — misfire accumulators. Mode 06 TIDs A2–A5, confirmed 2026-06-06.
FORD_20S: list[PIDConfig] = [
    PIDConfig(command=_FORD_MISFIRE_CYL1, table="ford_obd_20s", column="misfire_acc_cyl1", interval_s=20),
    PIDConfig(command=_FORD_MISFIRE_CYL2, table="ford_obd_20s", column="misfire_acc_cyl2", interval_s=20),
    PIDConfig(command=_FORD_MISFIRE_CYL3, table="ford_obd_20s", column="misfire_acc_cyl3", interval_s=20),
    PIDConfig(command=_FORD_MISFIRE_CYL4, table="ford_obd_20s", column="misfire_acc_cyl4", interval_s=20),
]


# ---------------------------------------------------------------------------
# ALL_PIDS — flat list of every active PIDConfig consumed by collector.py.
# ---------------------------------------------------------------------------

ALL_PIDS: list[PIDConfig] = [
    *STANDARD_1S,
    *STANDARD_5S,
    *STANDARD_30S,
    *FORD_5S,
    *FORD_10S,
    *FORD_20S,
]
