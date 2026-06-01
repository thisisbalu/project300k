"""Tests for collector.py — PID watcher registration, callback logic."""

import uuid
from unittest.mock import MagicMock, patch

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
# _make_callback() — buffer accumulation and combined-row flush logic
# ---------------------------------------------------------------------------

class TestMakeCallback:
    """Tests for the per-table buffer strategy.

    The collector accumulates PID values into a per-table buffer. When the
    table's interval_s elapses the whole buffer is flushed as one combined
    row — not one sparse row per PID.

    Setup pattern: most tests prime _table_buffer and _table_last_flush
    directly rather than going through start() so they can control timing
    without a real OBD connection.
    """

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

    def _init_table(self, collector, table, interval_s=1, last_flush=0.0):
        """Prime buffer state so callbacks don't KeyError."""
        collector._table_buffer[table] = {}
        collector._table_last_flush[table] = last_flush

    # --- guard clauses ---

    def test_skips_when_no_active_trip(self):
        c, mock_qw, _ = self._make_collector(trip_id=None)
        pid = self._make_pid()
        cb = c._make_callback(pid)
        cb(self._make_response(1500))
        mock_qw.enqueue.assert_not_called()

    def test_skips_fast_pids_when_paused(self):
        """1s and 5s PIDs must be suppressed when polling is paused."""
        c, mock_qw, _ = self._make_collector(trip_id=str(uuid.uuid4()), paused=True)
        pid = self._make_pid(interval_s=1)
        self._init_table(c, "obd_1s")
        cb = c._make_callback(pid)
        cb(self._make_response(800))
        mock_qw.enqueue.assert_not_called()

    def test_30s_pid_not_suppressed_when_paused(self):
        """30s tier (battery_v, fuel level) continues even when paused."""
        c, mock_qw, _ = self._make_collector(trip_id=str(uuid.uuid4()), paused=True)
        pid = self._make_pid(table="obd_30s", column="battery_v", interval_s=30)
        self._init_table(c, "obd_30s", interval_s=30, last_flush=0.0)
        cb = c._make_callback(pid)

        # First callback at t=100: elapsed=100 >= 30, buf empty → reset timer, no flush
        with patch("collector.time.monotonic", return_value=100.0):
            cb(self._make_response(14.2))
        mock_qw.enqueue.assert_not_called()

        # Second callback at t=131: elapsed=31 >= 30, buf has a value → flush
        with patch("collector.time.monotonic", return_value=131.0):
            cb(self._make_response(14.1))
        mock_qw.enqueue.assert_called_once()
        _, row = mock_qw.enqueue.call_args[0]
        assert row["battery_v"] == 14.2   # value from the completed window

    # --- core buffer behaviour ---

    def test_no_flush_before_interval_elapses(self):
        """Callback within the same window accumulates but does not flush."""
        c, mock_qw, _ = self._make_collector(trip_id="trip-1")
        pid = self._make_pid(table="obd_1s", column="rpm", interval_s=1)
        # last_flush=100 means the window is currently open
        self._init_table(c, "obd_1s", last_flush=100.0)

        with patch("collector.time.monotonic", return_value=100.5):  # 0.5s < 1s
            cb = c._make_callback(pid)
            cb(self._make_response(1200))

        mock_qw.enqueue.assert_not_called()
        assert c._table_buffer["obd_1s"]["rpm"] == 1200

    def test_flush_emits_combined_row_when_interval_elapses(self):
        """Buffer is flushed as one combined row containing all accumulated PIDs."""
        from obd_commands import PIDConfig
        import obd

        c, mock_qw, _ = self._make_collector(trip_id="trip-1")
        self._init_table(c, "obd_1s", last_flush=0.0)

        pid_rpm = self._make_pid(table="obd_1s", column="rpm", interval_s=1)
        pid_spd = PIDConfig(
            command=obd.commands.SPEED,
            table="obd_1s",
            column="speed_kmh",
            interval_s=1,
        )
        cb_rpm = c._make_callback(pid_rpm)
        cb_spd = c._make_callback(pid_spd)

        # First tick at t=100: elapsed=100 >= 1, buf empty → reset timer, no flush.
        # Both callbacks accumulate into the fresh window.
        with patch("collector.time.monotonic", return_value=100.0):
            cb_rpm(self._make_response(1200))
            cb_spd(self._make_response(50))
        mock_qw.enqueue.assert_not_called()
        assert c._table_buffer["obd_1s"] == {"rpm": 1200, "speed_kmh": 50}

        # Second tick at t=101.5: elapsed=1.5 >= 1 → flush previous window.
        with patch("collector.time.monotonic", return_value=101.5):
            cb_rpm(self._make_response(1300))
        mock_qw.enqueue.assert_called_once()
        table, row = mock_qw.enqueue.call_args[0]
        assert table == "obd_1s"
        assert row["rpm"] == 1200
        assert row["speed_kmh"] == 50
        assert row["synced"] == 0
        assert "trip_id" in row
        assert "timestamp" in row
        assert "id" in row

    def test_buffer_reset_after_flush(self):
        """Buffer is cleared after flush; new values go into the next window."""
        c, mock_qw, _ = self._make_collector(trip_id="trip-1")
        self._init_table(c, "obd_1s", last_flush=0.0)
        pid = self._make_pid(table="obd_1s", column="rpm", interval_s=1)
        cb = c._make_callback(pid)

        # First tick: reset timer (empty buf), accumulate rpm=1200
        with patch("collector.time.monotonic", return_value=100.0):
            cb(self._make_response(1200))

        # Second tick: flush {rpm:1200}, reset, accumulate rpm=1300
        with patch("collector.time.monotonic", return_value=101.5):
            cb(self._make_response(1300))

        # Third tick: flush {rpm:1300}
        with patch("collector.time.monotonic", return_value=103.0):
            cb(self._make_response(1400))

        assert mock_qw.enqueue.call_count == 2
        _, first_row = mock_qw.enqueue.call_args_list[0][0]
        _, second_row = mock_qw.enqueue.call_args_list[1][0]
        assert first_row["rpm"] == 1200
        assert second_row["rpm"] == 1300

    def test_last_flush_timestamp_updated_on_flush(self):
        """_table_last_flush is updated to now_mono whenever the window resets."""
        c, _, _ = self._make_collector(trip_id="trip-1")
        # Pre-populate buffer so the flush actually fires (non-empty check)
        c._table_buffer["obd_1s"] = {"speed_kmh": 40}
        c._table_last_flush["obd_1s"] = 98.0

        pid = self._make_pid(table="obd_1s", column="rpm", interval_s=1)
        with patch("collector.time.monotonic", return_value=100.0):  # 2s elapsed
            cb = c._make_callback(pid)
            cb(self._make_response(1200))

        assert c._table_last_flush["obd_1s"] == 100.0

    # --- NULL / error responses ---

    def test_null_response_stored_as_none_in_buffer(self):
        c, mock_qw, _ = self._make_collector(trip_id="trip-1")
        self._init_table(c, "obd_1s", last_flush=0.0)
        pid = self._make_pid(table="obd_1s", column="rpm", interval_s=1)
        cb = c._make_callback(pid)

        with patch("collector.time.monotonic", return_value=100.0):
            cb(self._make_response(is_null=True))
        assert c._table_buffer["obd_1s"]["rpm"] is None

        # Flush on next tick — None appears in the combined row
        with patch("collector.time.monotonic", return_value=101.5):
            cb(self._make_response(is_null=True))
        _, row = mock_qw.enqueue.call_args[0]
        assert row["rpm"] is None

    def test_none_response_stored_as_none_in_buffer(self):
        c, mock_qw, _ = self._make_collector(trip_id="trip-1")
        self._init_table(c, "obd_1s", last_flush=0.0)
        pid = self._make_pid(table="obd_1s", column="rpm", interval_s=1)
        cb = c._make_callback(pid)

        with patch("collector.time.monotonic", return_value=100.0):
            cb(None)
        assert c._table_buffer["obd_1s"]["rpm"] is None

        with patch("collector.time.monotonic", return_value=101.5):
            cb(None)
        _, row = mock_qw.enqueue.call_args[0]
        assert row["rpm"] is None

    def test_plain_value_without_magnitude_attr(self):
        """Response value without .magnitude attribute — use value directly."""
        c, mock_qw, _ = self._make_collector(trip_id="trip-1")
        self._init_table(c, "obd_1s", last_flush=0.0)
        pid = self._make_pid(table="obd_1s", column="rpm", interval_s=1)
        cb = c._make_callback(pid)

        r = MagicMock()
        r.is_null.return_value = False
        r.value = "raw-value"   # no .magnitude attribute

        with patch("collector.time.monotonic", return_value=100.0):
            cb(r)
        assert c._table_buffer["obd_1s"]["rpm"] == "raw-value"

    # --- query_sync() ---

    def test_query_sync_returns_none_when_not_connected(self):
        c, _, _ = self._make_collector(trip_id="t")
        c._async_conn = None
        import obd
        assert c.query_sync(obd.commands.GET_DTC) is None

    def test_query_sync_stops_loop_queries_and_restarts(self):
        c, _, _ = self._make_collector(trip_id="t")
        mock_async = MagicMock()
        mock_async.is_connected.return_value = True
        mock_resp = MagicMock()
        mock_async.query.return_value = mock_resp
        c._async_conn = mock_async

        import obd
        result = c.query_sync(obd.commands.GET_DTC)

        mock_async.stop.assert_called_once()
        mock_async.query.assert_called_once_with(obd.commands.GET_DTC)
        mock_async.start.assert_called_once()
        assert result is mock_resp

    def test_query_sync_restarts_loop_on_exception(self):
        c, _, _ = self._make_collector(trip_id="t")
        mock_async = MagicMock()
        mock_async.is_connected.return_value = True
        mock_async.query.side_effect = Exception("serial error")
        c._async_conn = mock_async

        import obd
        result = c.query_sync(obd.commands.GET_DTC)

        assert result is None
        # Loop must be restarted even after a query failure
        assert mock_async.start.call_count >= 1

    def test_no_flush_when_buffer_empty_at_window_boundary(self):
        """Empty buffer at window boundary resets timer but emits no row."""
        c, mock_qw, _ = self._make_collector(trip_id="trip-1")
        self._init_table(c, "obd_1s", last_flush=0.0)
        pid = self._make_pid(table="obd_1s", column="rpm", interval_s=1)

        with patch("collector.time.monotonic", return_value=100.0):
            cb = c._make_callback(pid)
            cb(self._make_response(1200))   # elapsed >> 1s but buf was empty

        mock_qw.enqueue.assert_not_called()
        assert c._table_last_flush["obd_1s"] == 100.0
