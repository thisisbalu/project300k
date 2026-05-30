"""Tests for collector.py — PID watcher registration, callback logic."""

import time
import uuid
from unittest.mock import MagicMock, call, patch

import pytest


@pytest.fixture
def mock_async_conn():
    return MagicMock()


@pytest.fixture
def collector(mock_async_conn):
    from collector import Collector
    mock_qw = MagicMock()
    mock_tm = MagicMock()
    mock_tm.current_trip_id = None
    mock_tm.is_paused = False
    mock_obd_conn = MagicMock()
    c = Collector(mock_qw, mock_tm, mock_obd_conn)
    return c, mock_qw, mock_tm


# ---------------------------------------------------------------------------
# start() / stop()
# ---------------------------------------------------------------------------

class TestStartStop:
    def _make_collector(self):
        from collector import Collector
        return Collector(MagicMock(), MagicMock(), MagicMock())

    def test_start_opens_async_connection(self):
        c = self._make_collector()
        with patch("collector.obd.Async") as mock_async_cls:
            mock_async_instance = MagicMock()
            mock_async_cls.return_value = mock_async_instance
            c.start()
            c.stop()

        mock_async_cls.assert_called_once()
        # fast=False is required on Pi
        _, kwargs = mock_async_cls.call_args
        assert kwargs.get("fast") is False

    def test_start_calls_async_start(self):
        c = self._make_collector()
        with patch("collector.obd.Async") as mock_async_cls:
            mock_instance = MagicMock()
            mock_async_cls.return_value = mock_instance
            c.start()
            c.stop()

        mock_instance.start.assert_called_once()

    def test_start_registers_watcher_for_each_pid(self):
        from obd_commands import ALL_PIDS
        c = self._make_collector()

        with patch("collector.obd.Async") as mock_async_cls:
            mock_instance = MagicMock()
            mock_async_cls.return_value = mock_instance
            c.start()
            c.stop()

        # ALL_PIDS watchers + RPM watcher + voltage watcher for trip detection
        expected_calls = len(ALL_PIDS) + 2
        assert mock_instance.watch.call_count == expected_calls

    def test_stop_calls_async_stop(self):
        c = self._make_collector()

        with patch("collector.obd.Async") as mock_async_cls:
            mock_instance = MagicMock()
            mock_async_cls.return_value = mock_instance
            c.start()
            c.stop()

        mock_instance.stop.assert_called_once()
        assert c._async_conn is None

    def test_stop_noop_when_not_started(self):
        c = self._make_collector()
        c.stop()  # must not raise


# ---------------------------------------------------------------------------
# _make_callback() — the closure logic
# ---------------------------------------------------------------------------

class TestMakeCallback:
    def _make_pid(self, table="obd_1s", column="rpm", interval_s=1):
        from obd_commands import PIDConfig
        import obd
        return PIDConfig(
            command=obd.commands.RPM,
            table=table,
            column=column,
            interval_s=interval_s,
        )

    def _make_response(self, magnitude=1500, is_null=False):
        r = MagicMock()
        r.is_null.return_value = is_null
        r.value.magnitude = magnitude
        return r

    def _make_collector(self, trip_id=None, paused=False):
        from collector import Collector
        mock_qw = MagicMock()
        mock_tm = MagicMock()
        mock_tm.current_trip_id = trip_id
        mock_tm.is_paused = paused
        mock_obd = MagicMock()
        c = Collector(mock_qw, mock_tm, mock_obd)
        return c, mock_qw, mock_tm

    def test_skips_enqueue_when_no_active_trip(self):
        c, mock_qw, _ = self._make_collector(trip_id=None)
        c._last_enqueue = {"RPM": 0.0}
        pid = self._make_pid()
        cb = c._make_callback(pid)
        cb(self._make_response(1500))
        mock_qw.enqueue.assert_not_called()

    def test_skips_fast_pids_when_paused(self):
        """1s and 5s PIDs must be suppressed when polling is paused."""
        c, mock_qw, _ = self._make_collector(trip_id=str(uuid.uuid4()), paused=True)
        pid_1s = self._make_pid(interval_s=1)
        c._last_enqueue = {pid_1s.command.name: 0.0}
        with patch("collector.time.monotonic", return_value=100.0):
            cb = c._make_callback(pid_1s)
            cb(self._make_response(800))
        mock_qw.enqueue.assert_not_called()

    def test_30s_pid_not_suppressed_when_paused(self):
        """30s tier (battery_v, fuel level) continues even when paused."""
        c, mock_qw, _ = self._make_collector(trip_id=str(uuid.uuid4()), paused=True)
        pid_30s = self._make_pid(interval_s=30)
        c._last_enqueue = {pid_30s.command.name: 0.0}
        with patch("collector.time.monotonic", return_value=100.0):
            cb = c._make_callback(pid_30s)
            cb(self._make_response(14.2))
        mock_qw.enqueue.assert_called_once()

    def test_enqueues_row_when_trip_active_and_interval_elapsed(self):
        c, mock_qw, _ = self._make_collector(trip_id=str(uuid.uuid4()))
        pid = self._make_pid(interval_s=1)
        c._last_enqueue = {pid.command.name: 0.0}

        with patch("collector.time.monotonic", return_value=100.0):
            cb = c._make_callback(pid)
            cb(self._make_response(2500))

        mock_qw.enqueue.assert_called_once()
        table, row = mock_qw.enqueue.call_args[0]
        assert table == "obd_1s"
        assert row["rpm"] == 2500
        assert row["synced"] == 0
        assert "trip_id" in row
        assert "timestamp" in row
        assert "id" in row

    def test_skips_enqueue_when_interval_not_elapsed(self):
        c, mock_qw, _ = self._make_collector(trip_id=str(uuid.uuid4()))
        pid = self._make_pid(interval_s=5)
        c._last_enqueue = {pid.command.name: 100.0}

        with patch("collector.time.monotonic", return_value=102.0):
            cb = c._make_callback(pid)
            cb(self._make_response(1000))

        mock_qw.enqueue.assert_not_called()

    def test_stores_none_for_null_response(self):
        c, mock_qw, _ = self._make_collector(trip_id=str(uuid.uuid4()))
        pid = self._make_pid()
        c._last_enqueue = {pid.command.name: 0.0}

        with patch("collector.time.monotonic", return_value=100.0):
            cb = c._make_callback(pid)
            cb(self._make_response(is_null=True))

        _, row = mock_qw.enqueue.call_args[0]
        assert row["rpm"] is None

    def test_stores_none_for_none_response(self):
        c, mock_qw, _ = self._make_collector(trip_id=str(uuid.uuid4()))
        pid = self._make_pid()
        c._last_enqueue = {pid.command.name: 0.0}

        with patch("collector.time.monotonic", return_value=100.0):
            cb = c._make_callback(pid)
            cb(None)

        _, row = mock_qw.enqueue.call_args[0]
        assert row["rpm"] is None

    def test_stores_plain_value_when_no_magnitude_attr(self):
        """Response value without .magnitude attribute — use value directly."""
        c, mock_qw, _ = self._make_collector(trip_id=str(uuid.uuid4()))
        pid = self._make_pid(column="rpm")
        c._last_enqueue = {pid.command.name: 0.0}

        r = MagicMock()
        r.is_null.return_value = False
        del r.value.magnitude
        r.value = "raw-value"

        with patch("collector.time.monotonic", return_value=100.0):
            cb = c._make_callback(pid)
            cb(r)

        _, row = mock_qw.enqueue.call_args[0]
        assert row["rpm"] == "raw-value"

    def test_updates_last_enqueue_time(self):
        c, _, _ = self._make_collector(trip_id=str(uuid.uuid4()))
        pid = self._make_pid(interval_s=1)
        c._last_enqueue = {pid.command.name: 0.0}

        with patch("collector.time.monotonic", return_value=99.5):
            cb = c._make_callback(pid)
            cb(self._make_response(800))

        assert c._last_enqueue[pid.command.name] == 99.5
