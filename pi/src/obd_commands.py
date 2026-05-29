"""
obd_commands.py — OBD PID definitions for the Bronco Sport 2.0L EcoBoost.

Organises all PIDs into polling tier groups consumed by collector.py.
Standard Mode 01 PIDs come from the python-obd built-in command table.
Ford Mode 22 enhanced PIDs require custom OBDCommand definitions and are
only available via OBDLink MX+ (not generic ELM327 adapters).

Polling tiers:
    STANDARD_1S   — 1s interval: core engine state (RPM, speed, throttle, load)
    STANDARD_5S   — 5s interval: thermals, air/fuel, O2 sensors
    STANDARD_30S  — 30s interval: battery voltage, fuel level

    FORD_5S       — 5s interval: transmission data (Mode 22, TCM module)
    FORD_10S      — 10s interval: boost and knock data (Mode 22, PCM module)
    FORD_20S      — 20s interval: misfire counters, fuel rail (Mode 22 + Mode 06)

Ford Mode 22 hex addresses confirmed via FORScan baseline scan on this VIN.
Addresses sourced from FORScan community + f150ecoboost.net forums — same
EcoBoost platform family. Verify all values during first live run.

References:
    Standard PIDs: SAE J1979 Mode 01
    Ford Mode 22:  Ford PCM/TCM proprietary, accessed via ISO 15765-4 CAN
    Mode 06:       SAE J1979 Mode 06, standardised misfire counters on CAN
"""

import obd


# ---------------------------------------------------------------------------
# Standard OBD-II Mode 01 — built into python-obd
# ---------------------------------------------------------------------------

STANDARD_1S = [
    obd.commands.RPM,           # Engine RPM — core health signal, idle drift = wear
    obd.commands.SPEED,         # Vehicle speed km/h — trip context
    obd.commands.THROTTLE_POS,  # Throttle position % — driver input
    obd.commands.ENGINE_LOAD,   # Calculated engine load % — how hard engine is working
]

STANDARD_5S = [
    obd.commands.COOLANT_TEMP,       # Engine coolant temperature °C — overheating, thermostat
    obd.commands.OIL_TEMP,           # Engine oil temperature °C — wear risk at high temp
    obd.commands.MAF,                # Mass air flow g/s — air intake health
    obd.commands.SHORT_FUEL_TRIM_1,  # Short term fuel trim % — real-time injector/O2 correction
    obd.commands.LONG_FUEL_TRIM_1,   # Long term fuel trim % — persistent correction, vacuum leak
    obd.commands.O2_B1S1,            # O2 sensor Bank1 Sensor1 V — pre-cat, combustion quality
    obd.commands.O2_B1S2,            # O2 sensor Bank1 Sensor2 V — post-cat, catalyst health
]

STANDARD_30S = [
    obd.commands.CONTROL_MODULE_VOLTAGE,  # Battery/alternator voltage V — charging system health
    obd.commands.FUEL_LEVEL,              # Fuel level % — trip context, consumption tracking
]


# ---------------------------------------------------------------------------
# Ford Mode 22 Enhanced PIDs — requires OBDLink MX+
# Populated after FORScan baseline scan confirms addresses on this VIN.
# ---------------------------------------------------------------------------

# TCM module — transmission data (5s interval)
# Expected PIDs: trans_temp_c (221E1C), trans_gear (221E12), tcc_ratio (221E15)
FORD_5S = []  # TODO: Task 7 — add OBDCommand objects after FORScan confirmation

# PCM module — boost and knock data (10s interval)
# Expected PIDs: knock_retard_deg (220318), boost_desired_psi (22033E),
#                boost_actual_psi (22D137), wastegate_pct (2203CA)
FORD_10S = []  # TODO: Task 7 — add OBDCommand objects after FORScan confirmation

# PCM module + Mode 06 — misfire counters and fuel rail (20s interval)
# Expected PIDs: misfire_cyl1-4 (Mode 06: 06A20C-06A50C),
#                fuel_rail_pressure_psi (Mode 22: address TBD from FORScan)
FORD_20S = []  # TODO: Task 7 — add OBDCommand objects after FORScan confirmation

# Mode 06 misfire counters — SAE-standardised on CAN, preferred over Mode 22 path
# Pattern: 06A[2-5]0C where A2=Cyl1, A3=Cyl2, A4=Cyl3, A5=Cyl4
MODE06_MISFIRE = []  # TODO: Task 7 — define Mode 06 OBDCommand objects
