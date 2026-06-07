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

    def test_contains_fuel_rail(self, standard_5s):
        columns = [p.column for p in standard_5s]
        assert "fuel_rail_kpa" in columns


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
# Ford PIDs — confirmed addresses from pid_log_20260605_190444.txt
# ---------------------------------------------------------------------------

def test_ford_5s_populated():
    from obd_commands import FORD_5S
    columns = {p.column for p in FORD_5S}
    assert "trans_temp_c" in columns
    assert "trans_oil_temp2_c" in columns
    assert "trans_line_pressure_kpa" in columns
    assert "trans_gear" in columns
    assert "tcc_ratio" in columns
    assert all(p.table == "ford_obd_5s" for p in FORD_5S)
    assert all(p.interval_s == 5 for p in FORD_5S)


def test_trans_gear_returns_none_for_park_state():
    """0x46 is the TCM park-state code — must store NULL, not 70."""
    from obd_commands import _mode22
    import unittest.mock as mock
    decoder = _mode22(1, lambda d: d[4] if 1 <= d[4] <= 8 else None)
    frame = mock.Mock()
    frame.data = bytes([0x04, 0x62, 0x1E, 0x12, 0x46])  # d[4]=0x46=Park
    msg = mock.Mock()
    msg.frames = [frame]
    assert decoder([msg]) is None


def test_trans_gear_returns_gear_during_driving():
    from obd_commands import _mode22
    import unittest.mock as mock
    decoder = _mode22(1, lambda d: d[4] if 1 <= d[4] <= 8 else None)
    for gear in range(1, 7):
        frame = mock.Mock()
        frame.data = bytes([0x04, 0x62, 0x1E, 0x12, gear])
        msg = mock.Mock()
        msg.frames = [frame]
        assert decoder([msg]) == gear


def test_tcc_ratio_returns_none_for_park_state():
    """0x46 in tcc_ratio = Park state code — must store NULL, not 0.275."""
    from obd_commands import _mode22
    import unittest.mock as mock
    decoder = _mode22(1, lambda d: round(d[4] / 255, 3) if d[4] != 0x46 else None)
    frame = mock.Mock()
    frame.data = bytes([0x04, 0x62, 0x1E, 0x1F, 0x46])
    msg = mock.Mock()
    msg.frames = [frame]
    assert decoder([msg]) is None


def test_tcc_ratio_locked():
    from obd_commands import _mode22
    import unittest.mock as mock
    decoder = _mode22(1, lambda d: round(d[4] / 255, 3) if d[4] != 0x46 else None)
    frame = mock.Mock()
    frame.data = bytes([0x04, 0x62, 0x1E, 0x1F, 0xFF])  # fully locked
    msg = mock.Mock()
    msg.frames = [frame]
    assert decoder([msg]) == 1.0


def test_ford_10s_populated():
    from obd_commands import FORD_10S
    columns = {p.column for p in FORD_10S}
    assert "oil_pressure_kpa" in columns
    assert "knock_retard_deg" in columns
    assert "boost_desired_psi" in columns
    assert "boost_actual_psi" in columns
    assert "cac_temp_c" in columns
    assert "wastegate_pct" in columns
    assert "vct_intake_deg" in columns
    assert all(p.table == "ford_obd_10s" for p in FORD_10S)
    assert all(p.interval_s == 10 for p in FORD_10S)


def test_ford_20s_populated():
    from obd_commands import FORD_20S
    columns = {p.column for p in FORD_20S}
    assert "misfire_acc_cyl1" in columns
    assert "misfire_acc_cyl2" in columns
    assert "misfire_acc_cyl3" in columns
    assert "misfire_acc_cyl4" in columns
    assert all(p.table == "ford_obd_20s" for p in FORD_20S)
    assert all(p.interval_s == 20 for p in FORD_20S)


def test_ford_mode06_decoder_returns_none_on_empty():
    from obd_commands import _mode06_misfire
    decoder = _mode06_misfire()
    assert decoder([]) is None


def test_ford_mode06_decoder_returns_none_on_wrong_svc():
    """Non-Mode-06 response must produce NULL."""
    from obd_commands import _mode06_misfire
    import unittest.mock as mock
    decoder = _mode06_misfire()
    frame = mock.Mock()
    frame.data = bytes([0x10, 0x25, 0x62, 0xA2, 0x0B, 0x24, 0x00, 0x05])  # 0x62 = Mode 22, not Mode 06
    msg = mock.Mock()
    msg.frames = [frame]
    assert decoder([msg]) is None


def test_ford_mode06_decoder_happy_path():
    """OBDMID=0x0B, count=5 should decode to 5."""
    from obd_commands import _mode06_misfire
    import unittest.mock as mock
    decoder = _mode06_misfire()
    frame = mock.Mock()
    frame.data = bytes([0x10, 0x25, 0x46, 0xA2, 0x0B, 0x24, 0x00, 0x05])
    msg = mock.Mock()
    msg.frames = [frame]
    assert decoder([msg]) == 5


def test_ford_mode06_decoder_happy_path_large_count():
    """16-bit count (e.g. 300 = 0x012C) must decode correctly."""
    from obd_commands import _mode06_misfire
    import unittest.mock as mock
    decoder = _mode06_misfire()
    frame = mock.Mock()
    frame.data = bytes([0x10, 0x25, 0x46, 0xA3, 0x0B, 0x24, 0x01, 0x2C])
    msg = mock.Mock()
    msg.frames = [frame]
    assert decoder([msg]) == 300


def test_ford_mode06_decoder_returns_none_on_wrong_obdmid():
    """Any OBDMID other than 0x0B in position data[4] must return None."""
    from obd_commands import _mode06_misfire
    import unittest.mock as mock
    decoder = _mode06_misfire()
    frame = mock.Mock()
    frame.data = bytes([0x10, 0x25, 0x46, 0xA2, 0xFF, 0x24, 0x00, 0x00])  # 0xFF = wrong OBDMID
    msg = mock.Mock()
    msg.frames = [frame]
    assert decoder([msg]) is None


def test_ford_mode22_decoder_returns_none_on_bad_response():
    from obd_commands import _mode22
    decoder = _mode22(2, lambda d: d[4])
    assert decoder([]) is None


def test_ford_mode22_decoder_returns_none_on_nrc():
    """NRC response (data[1] == 0x7F) should produce NULL, not a bad value."""
    from obd_commands import _mode22
    import unittest.mock as mock
    decoder = _mode22(2, lambda d: d[4])
    frame = mock.Mock()
    frame.data = bytes([0x03, 0x7F, 0x22, 0x31])
    msg = mock.Mock()
    msg.frames = [frame]
    assert decoder([msg]) is None


def test_ford_mode22_decoder_happy_path():
    from obd_commands import _mode22
    import unittest.mock as mock
    decoder = _mode22(2, lambda d: (d[4] * 256) + d[5])
    frame = mock.Mock()
    frame.data = bytes([0x05, 0x62, 0x04, 0x15, 0x01, 0x48])
    msg = mock.Mock()
    msg.frames = [frame]
    assert decoder([msg]) == 328  # 0x01 * 256 + 0x48 = 328 kPa


# ---------------------------------------------------------------------------
# ALL_PIDS contains all standard PIDs
# ---------------------------------------------------------------------------

def test_all_pids_includes_all_standard(all_pids, standard_1s, standard_5s, standard_30s):
    all_names = {p.command.name for p in all_pids}
    for pid in [*standard_1s, *standard_5s, *standard_30s]:
        assert pid.command.name in all_names
