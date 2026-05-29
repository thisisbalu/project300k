import obd

# Standard Mode 01 PIDs — built into python-obd
STANDARD_1S = [
    obd.commands.RPM,
    obd.commands.SPEED,
    obd.commands.THROTTLE_POS,
    obd.commands.ENGINE_LOAD,
]

STANDARD_5S = [
    obd.commands.COOLANT_TEMP,
    obd.commands.OIL_TEMP,
    obd.commands.MAF,
    obd.commands.SHORT_FUEL_TRIM_1,
    obd.commands.LONG_FUEL_TRIM_1,
    obd.commands.O2_B1S1,
    obd.commands.O2_B1S2,
]

STANDARD_30S = [
    obd.commands.CONTROL_MODULE_VOLTAGE,
    obd.commands.FUEL_LEVEL,
]

# Ford Mode 22 enhanced PIDs
# TODO: Task 7 — define OBDCommand objects with decoders after FORScan confirmation
FORD_5S = []   # trans_temp, trans_gear, tcc_ratio
FORD_10S = []  # knock_retard, boost_desired, boost_actual, wastegate
FORD_20S = []  # misfire_cyl1-4, fuel_rail_pressure

# Mode 06 misfire counters
# TODO: Task 7 — define Mode 06 OBDCommand objects
MODE06_MISFIRE = []  # 06A20C–06A50C (cylinders 1–4)
