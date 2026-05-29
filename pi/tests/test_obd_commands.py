"""Tests for obd_commands.py — PID list structure, no duplicates, correct tiers."""

import pytest


@pytest.fixture(scope="module")
def all_pids():
    from obd_commands import ALL_PIDS
    return ALL_PIDS


@pytest.fixture(scope="module")
def standard_1s():
    from obd_commands import STANDARD_1S
    return STANDARD_1S


@pytest.fixture(scope="module")
def standard_5s():
    from obd_commands import STANDARD_5S
    return STANDARD_5S


@pytest.fixture(scope="module")
def standard_30s():
    from obd_commands import STANDARD_30S
    return STANDARD_30S


# ---------------------------------------------------------------------------
# ALL_PIDS structural invariants
# ---------------------------------------------------------------------------

class TestAllPids:
    def test_no_duplicate_command_names(self, all_pids):
        names = [p.command.name for p in all_pids]
        assert len(names) == len(set(names)), f"Duplicate PIDs: {[n for n in names if names.count(n) > 1]}"

    def test_all_interval_s_positive(self, all_pids):
        for pid in all_pids:
            assert pid.interval_s > 0, f"{pid.command.name} has interval_s={pid.interval_s}"

    def test_all_tables_in_allowed_set(self, all_pids):
        from queue_writer import ALLOWED_TABLES
        for pid in all_pids:
            assert pid.table in ALLOWED_TABLES, f"{pid.command.name} routes to unknown table '{pid.table}'"

    def test_all_columns_nonempty(self, all_pids):
        for pid in all_pids:
            assert pid.column, f"{pid.command.name} has empty column"

    def test_all_commands_nonempty(self, all_pids):
        for pid in all_pids:
            assert pid.command is not None


# ---------------------------------------------------------------------------
# PIDConfig is frozen (immutable after creation)
# ---------------------------------------------------------------------------

def test_pidconfig_is_frozen():
    from obd_commands import PIDConfig, STANDARD_1S
    pid = STANDARD_1S[0]
    with pytest.raises((AttributeError, TypeError)):
        pid.interval_s = 999


# ---------------------------------------------------------------------------
# STANDARD_1S tier
# ---------------------------------------------------------------------------

class TestStandard1S:
    def test_all_interval_1s(self, standard_1s):
        for pid in standard_1s:
            assert pid.interval_s == 1

    def test_all_route_to_obd_1s_table(self, standard_1s):
        for pid in standard_1s:
            assert pid.table == "obd_1s"

    def test_contains_rpm(self, standard_1s):
        columns = [p.column for p in standard_1s]
        assert "rpm" in columns

    def test_contains_speed(self, standard_1s):
        columns = [p.column for p in standard_1s]
        assert "speed_kmh" in columns

    def test_contains_throttle(self, standard_1s):
        columns = [p.column for p in standard_1s]
        assert "throttle_pct" in columns

    def test_contains_load(self, standard_1s):
        columns = [p.column for p in standard_1s]
        assert "load_pct" in columns


# ---------------------------------------------------------------------------
# STANDARD_5S tier
# ---------------------------------------------------------------------------

class TestStandard5S:
    def test_all_interval_5s(self, standard_5s):
        for pid in standard_5s:
            assert pid.interval_s == 5

    def test_all_route_to_obd_5s_table(self, standard_5s):
        for pid in standard_5s:
            assert pid.table == "obd_5s"

    def test_contains_coolant_temp(self, standard_5s):
        columns = [p.column for p in standard_5s]
        assert "coolant_temp_c" in columns

    def test_contains_fuel_trims(self, standard_5s):
        columns = [p.column for p in standard_5s]
        assert "stft_pct" in columns
        assert "ltft_pct" in columns

    def test_contains_o2_sensors(self, standard_5s):
        columns = [p.column for p in standard_5s]
        assert "o2_b1s1_v" in columns
        assert "o2_b1s2_v" in columns


# ---------------------------------------------------------------------------
# STANDARD_30S tier
# ---------------------------------------------------------------------------

class TestStandard30S:
    def test_all_interval_30s(self, standard_30s):
        for pid in standard_30s:
            assert pid.interval_s == 30

    def test_all_route_to_obd_30s_table(self, standard_30s):
        for pid in standard_30s:
            assert pid.table == "obd_30s"

    def test_contains_battery_voltage(self, standard_30s):
        columns = [p.column for p in standard_30s]
        assert "battery_v" in columns

    def test_contains_fuel_level(self, standard_30s):
        columns = [p.column for p in standard_30s]
        assert "fuel_level_pct" in columns

    def test_contains_ambient_temp(self, standard_30s):
        columns = [p.column for p in standard_30s]
        assert "ambient_air_temp_c" in columns

    def test_contains_dtc_distance(self, standard_30s):
        columns = [p.column for p in standard_30s]
        assert "distance_since_dtc_cleared_km" in columns


# ---------------------------------------------------------------------------
# Ford lists are empty (pending FORScan)
# ---------------------------------------------------------------------------

def test_ford_lists_empty_pending_forscan():
    from obd_commands import FORD_5S, FORD_10S, FORD_20S
    assert FORD_5S == []
    assert FORD_10S == []
    assert FORD_20S == []


# ---------------------------------------------------------------------------
# ALL_PIDS contains all standard PIDs
# ---------------------------------------------------------------------------

def test_all_pids_includes_all_standard(all_pids, standard_1s, standard_5s, standard_30s):
    all_names = {p.command.name for p in all_pids}
    for pid in [*standard_1s, *standard_5s, *standard_30s]:
        assert pid.command.name in all_names
