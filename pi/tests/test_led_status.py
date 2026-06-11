"""
test_led_status.py — Status LED logic tests.

evaluate_state() is a pure function, so the entire colour/priority spec is
covered without GPIO. The DB-reader helpers are exercised against a real temp
SQLite database via the db_conn fixture. gpiozero is never imported — LedDriver
is not constructed here, matching the suite's hardware-free contract.
"""

from __future__ import annotations

import sqlite3
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import led_status as led
from config import config
from led_status import (
    A_AMBER, A_BLUE, A_GREEN, A_OFF, A_RED,
    B_BLUE, B_GREEN, B_MAGENTA, B_OFF,
    Signals, evaluate_state,
)


def _signals(**overrides) -> Signals:
    """A fully-healthy snapshot (=> green / off), with named overrides."""
    base = dict(
        collector_active=True,
        usb_mounted=True,
        bt_adapter_present=True,
        data_fresh=True,
        trip_active=False,
        pi_warning=False,
        sync_behind=False,
        dtc_present=False,
    )
    base.update(overrides)
    return Signals(**base)


# ── LED A — Pipeline ──────────────────────────────────────────────────────────

def test_a_healthy_is_green():
    assert evaluate_state(_signals())[0] is A_GREEN


def test_a_collector_down_is_off():
    # off beats everything, even faults
    assert evaluate_state(_signals(collector_active=False, usb_mounted=False))[0] is A_OFF


def test_a_usb_unmounted_is_red():
    assert evaluate_state(_signals(usb_mounted=False))[0] is A_RED


def test_a_bt_missing_is_red():
    assert evaluate_state(_signals(bt_adapter_present=False))[0] is A_RED


def test_a_stalled_during_trip_is_red():
    assert evaluate_state(_signals(trip_active=True, data_fresh=False))[0] is A_RED


def test_a_red_beats_amber():
    assert evaluate_state(_signals(usb_mounted=False, pi_warning=True))[0] is A_RED


def test_a_pi_warning_is_amber():
    assert evaluate_state(_signals(pi_warning=True))[0] is A_AMBER


def test_a_amber_beats_green():
    assert evaluate_state(_signals(data_fresh=True, pi_warning=True))[0] is A_AMBER


def test_a_parked_is_blue():
    # up, no fault, no warning, but no fresh data and no active trip
    assert evaluate_state(_signals(data_fresh=False))[0] is A_BLUE


# ── LED B — Attention ─────────────────────────────────────────────────────────

def test_b_idle_is_off():
    assert evaluate_state(_signals())[1] is B_OFF


def test_b_trip_active_is_green_blink():
    led_b = evaluate_state(_signals(trip_active=True))[1]
    assert led_b is B_GREEN
    assert led_b.blink is True


def test_b_sync_behind_is_blue():
    assert evaluate_state(_signals(sync_behind=True))[1] is B_BLUE


def test_b_dtc_is_magenta():
    assert evaluate_state(_signals(dtc_present=True))[1] is B_MAGENTA


def test_b_dtc_beats_backlog_and_trip():
    led_b = evaluate_state(_signals(dtc_present=True, sync_behind=True, trip_active=True))[1]
    assert led_b is B_MAGENTA


def test_b_backlog_beats_trip():
    led_b = evaluate_state(_signals(sync_behind=True, trip_active=True))[1]
    assert led_b is B_BLUE


def test_only_b_green_blinks():
    # No other named display should blink — a blink anywhere else would stutter.
    for display in (A_OFF, A_BLUE, A_GREEN, A_RED, A_AMBER, B_OFF, B_BLUE, B_MAGENTA):
        assert display.blink is False


# ── _parse_ts ─────────────────────────────────────────────────────────────────

def test_parse_ts_offset():
    dt = led._parse_ts("2026-06-11T12:00:00+00:00")
    assert dt == datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)


def test_parse_ts_z_suffix():
    dt = led._parse_ts("2026-06-11T12:00:00Z")
    assert dt == datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)


def test_parse_ts_naive_assumed_utc():
    dt = led._parse_ts("2026-06-11T12:00:00")
    assert dt.tzinfo == timezone.utc


@pytest.mark.parametrize("value", [None, "", "not-a-date"])
def test_parse_ts_bad_input(value):
    assert led._parse_ts(value) is None


# ── DB readers ────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _insert_obd_1s(conn, trip_id, ts, synced=0):
    conn.execute(
        "INSERT INTO obd_1s (id, trip_id, timestamp, synced) VALUES (?, ?, ?, ?)",
        (str(uuid.uuid4()), trip_id, ts, synced),
    )
    conn.commit()


def _insert_dtc(conn, trip_id, ts):
    conn.execute(
        "INSERT INTO dtc_events (id, trip_id, timestamp, code, synced) VALUES (?, ?, ?, ?, 0)",
        (str(uuid.uuid4()), trip_id, ts, "P0300"),
    )
    conn.commit()


def test_data_fresh_recent_row(db_conn, trip_row):
    now = _now()
    _insert_obd_1s(db_conn, trip_row, now.isoformat())
    assert led._data_fresh(db_conn, now) is True


def test_data_fresh_stale_row(db_conn, trip_row):
    now = _now()
    _insert_obd_1s(db_conn, trip_row, (now - timedelta(seconds=30)).isoformat())
    assert led._data_fresh(db_conn, now) is False


def test_data_fresh_empty_table(db_conn):
    assert led._data_fresh(db_conn, _now()) is False


def test_has_open_trip_true(db_conn, trip_row):
    # trip_row inserts a trip with end_time NULL
    assert led._has_open_trip(db_conn) is True


def test_has_open_trip_false_when_closed(db_conn, trip_row):
    db_conn.execute(
        "UPDATE trips SET end_time = ? WHERE id = ?",
        ("2026-01-01T01:00:00+00:00", trip_row),
    )
    db_conn.commit()
    assert led._has_open_trip(db_conn) is False


def test_has_recent_dtc_true(db_conn, trip_row):
    now = _now()
    _insert_dtc(db_conn, trip_row, now.isoformat())
    assert led._has_recent_dtc(db_conn, now) is True


def test_has_recent_dtc_old(db_conn, trip_row):
    now = _now()
    _insert_dtc(db_conn, trip_row, (now - timedelta(days=30)).isoformat())
    assert led._has_recent_dtc(db_conn, now) is False


def test_has_recent_dtc_none(db_conn):
    assert led._has_recent_dtc(db_conn, _now()) is False


def test_sync_behind_old_unsynced(db_conn, trip_row):
    now = _now()
    _insert_obd_1s(db_conn, trip_row, (now - timedelta(days=15)).isoformat(), synced=0)
    assert led._is_sync_behind(db_conn, now) is True


def test_sync_behind_recent_unsynced(db_conn, trip_row):
    now = _now()
    _insert_obd_1s(db_conn, trip_row, (now - timedelta(minutes=2)).isoformat(), synced=0)
    assert led._is_sync_behind(db_conn, now) is False


def test_sync_behind_ignores_synced(db_conn, trip_row):
    now = _now()
    _insert_obd_1s(db_conn, trip_row, (now - timedelta(days=15)).isoformat(), synced=1)
    assert led._is_sync_behind(db_conn, now) is False


def test_sync_behind_empty(db_conn):
    assert led._is_sync_behind(db_conn, _now()) is False


# ── _collector_active ─────────────────────────────────────────────────────────

def test_collector_active_true(monkeypatch):
    monkeypatch.setattr(led.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout="active\n"))
    assert led._collector_active() is True


def test_collector_active_false(monkeypatch):
    monkeypatch.setattr(led.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout="inactive\n"))
    assert led._collector_active() is False


def test_collector_active_error(monkeypatch):
    def boom(*a, **k):
        raise OSError("systemctl missing")
    monkeypatch.setattr(led.subprocess, "run", boom)
    assert led._collector_active() is False


# ── _open_db ──────────────────────────────────────────────────────────────────

def test_open_db_bad_path_returns_none(monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", "/nonexistent/dir/obd.db")
    assert led._open_db() is None


# ── _pi_warning ───────────────────────────────────────────────────────────────

def test_pi_warning_hot_cpu(monkeypatch):
    monkeypatch.setattr(led.health, "_read_cpu_temp", lambda: 90.0)
    assert led._pi_warning(usb_mounted=False) is True


def test_pi_warning_rtc_bad(monkeypatch):
    monkeypatch.setattr(led.health, "_read_cpu_temp", lambda: 40.0)
    monkeypatch.setattr(led.health, "read_rtc_ok", lambda: 0)
    assert led._pi_warning(usb_mounted=False) is True


def test_pi_warning_low_disk(monkeypatch):
    monkeypatch.setattr(led.health, "_read_cpu_temp", lambda: 40.0)
    monkeypatch.setattr(led.health, "read_rtc_ok", lambda: 1)
    monkeypatch.setattr(led.psutil, "disk_usage",
                        lambda p: types.SimpleNamespace(free=10 * 1024 * 1024))
    assert led._pi_warning(usb_mounted=True) is True


def test_pi_warning_none(monkeypatch):
    monkeypatch.setattr(led.health, "_read_cpu_temp", lambda: 40.0)
    monkeypatch.setattr(led.health, "read_rtc_ok", lambda: 1)
    monkeypatch.setattr(led.psutil, "disk_usage",
                        lambda p: types.SimpleNamespace(free=5000 * 1024 * 1024))
    assert led._pi_warning(usb_mounted=True) is False


# ── read_signals ──────────────────────────────────────────────────────────────

def test_read_signals_with_db(monkeypatch, tmp_path):
    from storage import init_schema

    dbfile = tmp_path / "obd.db"
    conn = sqlite3.connect(str(dbfile))
    init_schema(conn)
    now = _now()
    tid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO trips (id, trip_number, start_time, synced) VALUES (?, 1, ?, 0)",
        (tid, now.isoformat()),
    )
    conn.execute(
        "INSERT INTO obd_1s (id, trip_id, timestamp, synced) VALUES (?, ?, ?, 0)",
        (str(uuid.uuid4()), tid, now.isoformat()),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(config, "DB_PATH", str(dbfile))
    monkeypatch.setattr(led.health, "_check_usb_mounted", lambda: 1)
    monkeypatch.setattr(led.health, "_check_bt_adapter", lambda: 1)
    monkeypatch.setattr(led, "_collector_active", lambda: True)
    monkeypatch.setattr(led, "_pi_warning", lambda usb_mounted: False)

    s = led.read_signals()
    assert s.collector_active and s.usb_mounted and s.bt_adapter_present
    assert s.data_fresh and s.trip_active
    assert not s.sync_behind and not s.dtc_present


def test_read_signals_usb_unmounted_skips_db(monkeypatch):
    monkeypatch.setattr(led.health, "_check_usb_mounted", lambda: 0)
    monkeypatch.setattr(led.health, "_check_bt_adapter", lambda: 0)
    monkeypatch.setattr(led, "_collector_active", lambda: False)
    monkeypatch.setattr(led, "_pi_warning", lambda usb_mounted: False)

    s = led.read_signals()
    assert not s.usb_mounted
    assert not s.data_fresh and not s.trip_active and not s.sync_behind and not s.dtc_present


# ── LedDriver (gpiozero mocked) ───────────────────────────────────────────────

class _FakeRGBLED:
    """Records colour/blink/off/close calls in place of gpiozero.RGBLED."""

    instances: list = []

    def __init__(self, red, green, blue, active_high, pwm):
        self.pins = (red, green, blue)
        self.active_high = active_high
        self.color = None
        self.blink_calls: list = []
        self.off_called = False
        self.closed = False
        _FakeRGBLED.instances.append(self)

    def blink(self, **kw):
        self.blink_calls.append(kw)

    def off(self):
        self.off_called = True

    def close(self):
        self.closed = True


@pytest.fixture
def fake_gpiozero(monkeypatch):
    _FakeRGBLED.instances = []
    mod = types.ModuleType("gpiozero")
    mod.RGBLED = _FakeRGBLED
    monkeypatch.setitem(sys.modules, "gpiozero", mod)
    return _FakeRGBLED


def test_driver_pins_and_polarity_from_config(fake_gpiozero):
    led.LedDriver()
    a, b = fake_gpiozero.instances
    assert a.pins == (config.LED_A_R, config.LED_A_G, config.LED_A_B)
    assert b.pins == (config.LED_B_R, config.LED_B_G, config.LED_B_B)
    assert a.active_high is True and b.active_high is True


def test_driver_sets_solid_color(fake_gpiozero):
    d = led.LedDriver()
    d.apply(A_GREEN, B_OFF)
    a, b = fake_gpiozero.instances
    assert a.color == led.GREEN
    assert b.color == led.OFF


def test_driver_blinks_b_green(fake_gpiozero):
    d = led.LedDriver()
    d.apply(A_GREEN, B_GREEN)
    _, b = fake_gpiozero.instances
    assert b.blink_calls and b.blink_calls[0]["on_color"] == led.GREEN


def test_driver_skips_unchanged_state(fake_gpiozero):
    d = led.LedDriver()
    d.apply(A_GREEN, B_OFF)
    a, _ = fake_gpiozero.instances
    a.color = "SENTINEL"
    d.apply(A_GREEN, B_OFF)
    assert a.color == "SENTINEL"  # unchanged Display => no re-write


def test_driver_off_releases_pins(fake_gpiozero):
    d = led.LedDriver()
    d.off()
    a, b = fake_gpiozero.instances
    assert a.off_called and a.closed and b.off_called and b.closed


def test_test_cycle_ends_on_blue(fake_gpiozero, monkeypatch):
    monkeypatch.setattr(led.time, "sleep", lambda s: None)
    d = led.LedDriver()
    led._test_cycle(d)
    a, b = fake_gpiozero.instances
    assert a.color == led.BLUE and b.color == led.OFF


# ── LedStatus loop ────────────────────────────────────────────────────────────

def test_ledstatus_applies_then_stops(monkeypatch):
    monkeypatch.setattr(led, "read_signals", lambda: _signals())
    recorded = []
    app = None

    class FakeDriver:
        def apply(self, led_a, led_b):
            recorded.append((led_a, led_b))
            app.stop()

    app = led.LedStatus(FakeDriver())
    app.run()
    assert recorded == [(A_GREEN, B_OFF)]


def test_ledstatus_survives_read_error(monkeypatch):
    def boom():
        raise RuntimeError("transient")
    monkeypatch.setattr(led, "read_signals", boom)

    app = led.LedStatus(driver=object())
    monkeypatch.setattr(app._stop, "wait", lambda timeout: app._stop.set())
    app.run()  # must not raise — the except branch swallows it
