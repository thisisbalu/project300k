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
    c = Collector(mock_qw, mock_tm)
    return c, mock_qw, mock_tm


# ---------------------------------------------------------------------------
# start() / stop()
# ---------------------------------------------------------------------------

class TestStartStop:
    def test_start_opens_async_connection(self):
        from collector import Collector
        mock_qw = MagicMock()
        mock_tm = MagicMock()
        c = Collector(mock_qw, mock_tm)

        with patch("collector.obd.Async") as mock_async_cls:
            mock_async_instance = MagicMock()
            mock_async_cls.return_value = mock_async_instance
            c.start()

        mock_async_cls.assert_called_once()
        # fast=False is required on Pi
        _, kwargs = mock_async_cls.call_args
        assert kwargs.get("fast") is False

    def test_start_calls_async_start(self):
        from collector import Collector
        from config import config
        mock_qw = MagicMock()
        mock_tm = MagicMock()
        c = Collector(mock_qw, mock_tm)

        with patch("collector.obd.Async") as mock_async_cls:
            mock_instance = MagicMock()
            mock_async_cls.return_value = mock_instance
            c.start()

        mock_instance.start.assert_called_once()

    def test_start_registers_watcher_for_each_pid(self):
        from collector import Collector
        from obd_commands import ALL_PIDS
        mock_qw = MagicMock()
        mock_tm = MagicMock()
        c = Collector(mock_qw, mock_tm)

        with patch("collector.obd.Async") as mock_async_cls:
            mock_instance = MagicMock()
            mock_async_cls.return_value = mock_instance
            c.start()

        # ALL_PIDS watchers + RPM watcher + voltage watcher for trip detection
        expected_calls = len(ALL_PIDS) + 2
        assert mock_instance.watch.call_count == expected_calls

    def test_stop_calls_async_stop(self):
        from collector import Collector
        mock_qw = MagicMock()
        mock_tm = MagicMock()
        c = Collector(mock_qw, mock_tm)

        with patch("collector.obd.Async") as mock_async_cls:
            mock_instance = MagicMock()
            mock_async_cls.return_value = mock_instance
            c.start()
            c.stop()

        mock_instance.stop.assert_called_once()
        assert c._async_conn is None

    def test_stop_noop_when_not_started(self):
        from collector import Collector
        c = Collector(MagicMock(), MagicMock())
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

    def test_skips_enqueue_when_no_active_trip(self):
        from collector import Collector
        mock_qw = MagicMock()
        mock_tm = MagicMock()
        mock_tm.current_trip_id = None
        c = Collector(mock_qw, mock_tm)
        c._last_enqueue = {obd_cmd_name: 0.0 for obd_cmd_name in ["RPM"]}

        pid = self._make_pid()
        cb = c._make_callback(pid)
        cb(self._make_response(1500))

        mock_qw.enqueue.assert_not_called()

    def test_enqueues_row_when_trip_active_and_interval_elapsed(self):
        from collector import Collector
        mock_qw = MagicMock()
        mock_tm = MagicMock()
        mock_tm.current_trip_id = str(uuid.uuid4())
        c = Collector(mock_qw, mock_tm)
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
        from collector import Collector
        mock_qw = MagicMock()
        mock_tm = MagicMock()
        mock_tm.current_trip_id = str(uuid.uuid4())
        c = Collector(mock_qw, mock_tm)
        pid = self._make_pid(interval_s=5)
        c._last_enqueue = {pid.command.name: 100.0}

        with patch("collector.time.monotonic", return_value=102.0):  # only 2s elapsed, need 5
            cb = c._make_callback(pid)
            cb(self._make_response(1000))

        mock_qw.enqueue.assert_not_called()

    def test_stores_none_for_null_response(self):
        from collector import Collector
        mock_qw = MagicMock()
        mock_tm = MagicMock()
        mock_tm.current_trip_id = str(uuid.uuid4())
        c = Collector(mock_qw, mock_tm)
        pid = self._make_pid()
        c._last_enqueue = {pid.command.name: 0.0}

        with patch("collector.time.monotonic", return_value=100.0):
            cb = c._make_callback(pid)
            cb(self._make_response(is_null=True))

        _, row = mock_qw.enqueue.call_args[0]
        assert row["rpm"] is None

    def test_stores_none_for_none_response(self):
        from collector import Collector
        mock_qw = MagicMock()
        mock_tm = MagicMock()
        mock_tm.current_trip_id = str(uuid.uuid4())
        c = Collector(mock_qw, mock_tm)
        pid = self._make_pid()
        c._last_enqueue = {pid.command.name: 0.0}

        with patch("collector.time.monotonic", return_value=100.0):
            cb = c._make_callback(pid)
            cb(None)

        _, row = mock_qw.enqueue.call_args[0]
        assert row["rpm"] is None

    def test_stores_plain_value_when_no_magnitude_attr(self):
        """Response value without .magnitude attribute — use value directly."""
        from collector import Collector
        mock_qw = MagicMock()
        mock_tm = MagicMock()
        mock_tm.current_trip_id = str(uuid.uuid4())
        c = Collector(mock_qw, mock_tm)
        pid = self._make_pid(column="rpm")
        c._last_enqueue = {pid.command.name: 0.0}

        r = MagicMock()
        r.is_null.return_value = False
        del r.value.magnitude  # remove magnitude attribute
        r.value = "raw-value"

        with patch("collector.time.monotonic", return_value=100.0):
            cb = c._make_callback(pid)
            cb(r)

        _, row = mock_qw.enqueue.call_args[0]
        assert row["rpm"] == "raw-value"

    def test_updates_last_enqueue_time(self):
        from collector import Collector
        mock_qw = MagicMock()
        mock_tm = MagicMock()
        mock_tm.current_trip_id = str(uuid.uuid4())
        c = Collector(mock_qw, mock_tm)
        pid = self._make_pid(interval_s=1)
        c._last_enqueue = {pid.command.name: 0.0}

        with patch("collector.time.monotonic", return_value=99.5):
            cb = c._make_callback(pid)
            cb(self._make_response(800))

        assert c._last_enqueue[pid.command.name] == 99.5
