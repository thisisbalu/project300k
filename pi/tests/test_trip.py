"""Tests for trip.py — TripManager state machine, threading, DTC dispatch."""

import threading
import time
import uuid
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rpm_response(rpm):
    """Build a mock OBDResponse for an RPM reading."""
    r = MagicMock()
    r.is_null.return_value = False
    r.value.magnitude = rpm
    return r


def _voltage_response(volts):
    """Build a mock OBDResponse for a voltage reading."""
    r = MagicMock()
    r.is_null.return_value = False
    r.value.magnitude = volts
    return r


def _null_response():
    r = MagicMock()
    r.is_null.return_value = True
    return r


@pytest.fixture
def tm(db_conn):
    """TripManager with mocked QueueWriter."""
    from trip import TripManager

    mock_qw = MagicMock()
    mock_qw.conn = db_conn

    return TripManager(mock_qw)


# ---------------------------------------------------------------------------
# on_voltage() — stores last voltage, ignores null
# ---------------------------------------------------------------------------

class TestOnVoltage:
    def test_stores_voltage(self, tm):
        tm.on_voltage(_voltage_response(14.2))
        assert tm._last_voltage == 14.2

    def test_null_response_ignored(self, tm):
        tm.on_voltage(_null_response())
        assert tm._last_voltage is None

    def test_none_response_ignored(self, tm):
        tm.on_voltage(None)
        assert tm._last_voltage is None

    def test_overwrites_previous_voltage(self, tm):
        tm.on_voltage(_voltage_response(13.5))
        tm.on_voltage(_voltage_response(12.1))
        assert tm._last_voltage == 12.1


# ---------------------------------------------------------------------------
# _voltage_above() / _voltage_below()
# ---------------------------------------------------------------------------

class TestVoltageHelpers:
    def test_voltage_above_returns_false_when_none(self, tm):
        assert tm._voltage_above(13.0) is False

    def test_voltage_above_returns_true_when_above(self, tm):
        tm._last_voltage = 13.5
        assert tm._voltage_above(13.0) is True

    def test_voltage_above_returns_false_when_equal(self, tm):
        tm._last_voltage = 13.0
        assert tm._voltage_above(13.0) is False

    def test_voltage_below_returns_false_when_none(self, tm):
        assert tm._voltage_below(12.5) is False

    def test_voltage_below_returns_true_when_below(self, tm):
        tm._last_voltage = 12.0
        assert tm._voltage_below(12.5) is True

    def test_voltage_below_returns_false_when_equal(self, tm):
        tm._last_voltage = 12.5
        assert tm._voltage_below(12.5) is False


# ---------------------------------------------------------------------------
# on_rpm() — null and None guards
# ---------------------------------------------------------------------------

class TestOnRpmGuards:
    def test_none_response_returns_immediately(self, tm):
        tm.on_rpm(None)
        assert tm.current_trip_id is None
        assert tm._rpm_zero_since is None

    def test_null_response_returns_immediately(self, tm):
        tm.on_rpm(_null_response())
        assert tm.current_trip_id is None


# ---------------------------------------------------------------------------
# Trip start conditions
# ---------------------------------------------------------------------------

class TestTripStart:
    def test_starts_trip_when_rpm_gt0_and_voltage_above_13(self, tm):
        with patch("trip.get_trip_number", return_value=1):
            tm.on_voltage(_voltage_response(13.5))
            tm.on_rpm(_rpm_response(800))
        assert tm.current_trip_id is not None

    def test_no_trip_start_when_rpm_gt0_but_voltage_low(self, tm):
        tm.on_voltage(_voltage_response(12.0))
        tm.on_rpm(_rpm_response(800))
        assert tm.current_trip_id is None

    def test_no_trip_start_when_voltage_ok_but_rpm_zero(self, tm):
        tm.on_voltage(_voltage_response(13.5))
        tm.on_rpm(_rpm_response(0))
        assert tm.current_trip_id is None

    def test_no_duplicate_start_when_trip_already_active(self, tm):
        with patch("trip.get_trip_number", return_value=1):
            tm.on_voltage(_voltage_response(14.0))
            tm.on_rpm(_rpm_response(1000))
            first_id = tm.current_trip_id
            tm.on_rpm(_rpm_response(1200))
            assert tm.current_trip_id == first_id

    def test_start_enqueues_trips_row(self, tm):
        with patch("trip.get_trip_number", return_value=3):
            tm.on_voltage(_voltage_response(13.8))
            tm.on_rpm(_rpm_response(2000))

        tm._queue_writer.enqueue.assert_called_once()
        table, row = tm._queue_writer.enqueue.call_args[0]
        assert table == "trips"
        assert row["trip_number"] == 3
        assert row["synced"] == 0
        assert row["start_time"] is not None

    def test_start_dispatches_dtc_scan(self, tm):
        with patch("trip.get_trip_number", return_value=1), \
             patch.object(tm, "_dispatch_dtc_scan") as mock_dispatch:
            tm.on_voltage(_voltage_response(14.0))
            tm.on_rpm(_rpm_response(500))
            mock_dispatch.assert_called_once_with(tm.current_trip_id, "trip_start")

    def test_no_trip_start_without_any_voltage_reading(self, tm):
        """No voltage reading means _last_voltage is None → _voltage_above returns False."""
        tm.on_rpm(_rpm_response(1500))
        assert tm.current_trip_id is None


# ---------------------------------------------------------------------------
# Trip end conditions
# ---------------------------------------------------------------------------

class TestTripEnd:
    def _start_trip(self, tm):
        with patch("trip.get_trip_number", return_value=1):
            tm.on_voltage(_voltage_response(14.0))
            tm.on_rpm(_rpm_response(1000))
        return tm.current_trip_id

    def test_trip_ends_after_30s_rpm0_and_voltage_below_12_5(self, tm):
        trip_id = self._start_trip(tm)
        assert trip_id is not None

        with patch("trip.time.monotonic") as mock_mono, \
             patch("trip.update_trip_end") as mock_end, \
             patch.object(tm, "_dispatch_dtc_scan"):
            mock_mono.return_value = 100.0
            tm.on_voltage(_voltage_response(12.0))
            tm.on_rpm(_rpm_response(0))  # sets _rpm_zero_since = 100.0

            mock_mono.return_value = 131.0  # 31s later
            tm.on_rpm(_rpm_response(0))

        assert tm.current_trip_id is None
        mock_end.assert_called_once()
        # update_trip_end(queue_writer, trip_id, end_time)
        args = mock_end.call_args[0]
        assert args[1] == trip_id

    def test_trip_not_ended_when_only_rpm0_without_voltage_drop(self, tm):
        self._start_trip(tm)

        with patch("trip.time.monotonic") as mock_mono:
            mock_mono.return_value = 100.0
            tm.on_voltage(_voltage_response(13.8))  # still running
            tm.on_rpm(_rpm_response(0))

            mock_mono.return_value = 131.0
            tm.on_rpm(_rpm_response(0))

        assert tm.current_trip_id is not None

    def test_trip_not_ended_before_30s_threshold(self, tm):
        self._start_trip(tm)

        with patch("trip.time.monotonic") as mock_mono:
            mock_mono.return_value = 100.0
            tm.on_voltage(_voltage_response(12.0))
            tm.on_rpm(_rpm_response(0))

            mock_mono.return_value = 115.0  # only 15s
            tm.on_rpm(_rpm_response(0))

        assert tm.current_trip_id is not None

    def test_trip_end_dispatches_dtc_scan(self, tm):
        trip_id = self._start_trip(tm)

        with patch("trip.time.monotonic") as mock_mono, \
             patch("trip.update_trip_end"), \
             patch.object(tm, "_dispatch_dtc_scan") as mock_dispatch:
            mock_mono.return_value = 100.0
            tm.on_voltage(_voltage_response(12.0))
            tm.on_rpm(_rpm_response(0))
            mock_mono.return_value = 131.0
            tm.on_rpm(_rpm_response(0))

        # First call was trip_start, second should be trip_end
        calls = mock_dispatch.call_args_list
        end_call = [c for c in calls if c[0][1] == "trip_end"]
        assert len(end_call) == 1
        assert end_call[0][0][0] == trip_id


# ---------------------------------------------------------------------------
# Polling pause / resume
# ---------------------------------------------------------------------------

class TestPollingPause:
    def test_polling_paused_after_30s_rpm0(self, tm):
        with patch("trip.time.monotonic") as mock_mono:
            mock_mono.return_value = 100.0
            tm.on_rpm(_rpm_response(0))
            mock_mono.return_value = 131.0
            tm.on_rpm(_rpm_response(0))
        assert tm._polling_paused is True

    def test_polling_resumed_when_rpm_gt0(self, tm):
        tm._polling_paused = True
        tm.on_rpm(_rpm_response(1500))
        assert tm._polling_paused is False

    def test_pause_not_applied_twice(self, tm, caplog):
        import logging
        with patch("trip.time.monotonic") as mock_mono:
            mock_mono.return_value = 100.0
            tm.on_rpm(_rpm_response(0))
            mock_mono.return_value = 131.0
            with caplog.at_level(logging.INFO, logger="obd-collector"):
                tm.on_rpm(_rpm_response(0))  # triggers pause
            pause_count = caplog.text.count("Polling paused")

            mock_mono.return_value = 135.0
            caplog.clear()
            with caplog.at_level(logging.INFO, logger="obd-collector"):
                tm.on_rpm(_rpm_response(0))  # already paused, must not log again
            assert "Polling paused" not in caplog.text

    def test_rpm_zero_since_reset_on_rpm_gt0(self, tm):
        with patch("trip.time.monotonic", return_value=100.0):
            tm.on_rpm(_rpm_response(0))
        assert tm._rpm_zero_since is not None
        tm.on_rpm(_rpm_response(1000))
        assert tm._rpm_zero_since is None


# ---------------------------------------------------------------------------
# _dispatch_dtc_scan() — starts daemon thread
# ---------------------------------------------------------------------------

class TestTripManagerStop:
    def test_stop_joins_dtc_threads(self, tm):
        """stop() waits for in-flight DTC threads before returning."""
        finished = []
        def slow_scan(trip_id, trigger):
            import time as _t
            _t.sleep(0.05)
            finished.append(True)

        with patch.object(tm, "_scan_dtc", side_effect=slow_scan):
            tm._dispatch_dtc_scan("fake-id", "trip_end")
        tm.stop()
        assert finished  # thread completed before stop() returned

    def test_is_paused_property_reflects_state(self, tm):
        assert tm.is_paused is False
        tm._polling_paused = True
        assert tm.is_paused is True


class TestDispatchDtcScan:
    def test_dispatch_starts_daemon_thread(self, tm):
        started = threading.Event()
        original_start = threading.Thread.start

        def fake_start(self_thread):
            started.set()

        with patch.object(threading.Thread, "start", fake_start):
            tm._dispatch_dtc_scan("fake-trip-id", "trip_start")
        assert started.is_set()

    def test_scan_skipped_when_dtc_query_not_wired(self, tm, caplog):
        import logging
        # _dtc_query_fn defaults to None — scan is skipped before collector is wired
        with caplog.at_level(logging.WARNING, logger="obd-collector"):
            tm._scan_dtc("fake-id", "trip_start")
        assert "DTC scan skipped" in caplog.text

    def test_scan_skipped_when_query_returns_none(self, tm, caplog):
        import logging
        # query_sync returns None when the async connection is not available
        tm._dtc_query_fn = MagicMock(return_value=None)
        with caplog.at_level(logging.WARNING, logger="obd-collector"):
            tm._scan_dtc("fake-id", "trip_start")
        assert "DTC scan skipped" in caplog.text

    def test_scan_logs_clean_when_no_dtcs(self, tm, caplog):
        import logging
        clean = MagicMock()
        clean.is_null.return_value = False
        clean.value = []
        # Same empty response returned for both Mode 03 and Mode 07
        tm._dtc_query_fn = MagicMock(return_value=clean)
        with caplog.at_level(logging.INFO, logger="obd-collector"):
            tm._scan_dtc("fake-id", "trip_start")
        assert "clean" in caplog.text

    def test_scan_enqueues_stored_dtc_rows(self, tm):
        stored_resp = MagicMock()
        stored_resp.is_null.return_value = False
        stored_resp.value = [("P0300", "Random Misfire"), ("P0171", "Lean")]
        pending_resp = MagicMock()
        pending_resp.is_null.return_value = False
        pending_resp.value = []
        tm._dtc_query_fn = MagicMock(side_effect=[stored_resp, pending_resp])
        tm._scan_dtc("fake-id", "trip_end")
        assert tm._queue_writer.enqueue.call_count == 2
        _, row = tm._queue_writer.enqueue.call_args_list[0][0]
        assert row["code"] == "P0300"
        assert row["scan_trigger"] == "trip_end"
        assert row["status"] == "stored"

    def test_scan_enqueues_pending_dtc_rows(self, tm):
        stored_resp = MagicMock()
        stored_resp.is_null.return_value = False
        stored_resp.value = []
        pending_resp = MagicMock()
        pending_resp.is_null.return_value = False
        pending_resp.value = [("P0420", "Catalyst Efficiency Below Threshold")]
        tm._dtc_query_fn = MagicMock(side_effect=[stored_resp, pending_resp])
        tm._scan_dtc("fake-id", "trip_start")
        assert tm._queue_writer.enqueue.call_count == 1
        _, row = tm._queue_writer.enqueue.call_args[0]
        assert row["code"] == "P0420"
        assert row["status"] == "pending"
        assert row["scan_trigger"] == "trip_start"

    def test_scan_null_response_logs_clean(self, tm, caplog):
        import logging
        mock_resp = MagicMock()
        mock_resp.is_null.return_value = True
        tm._dtc_query_fn = MagicMock(return_value=mock_resp)
        with caplog.at_level(logging.INFO, logger="obd-collector"):
            tm._scan_dtc("fake-id", "trip_start")
        assert "clean" in caplog.text

    def test_scan_exception_logged(self, tm, caplog):
        import logging
        tm._dtc_query_fn = MagicMock(side_effect=Exception("serial error"))
        with caplog.at_level(logging.ERROR, logger="obd-collector"):
            tm._scan_dtc("fake-id", "trip_end")
        assert "DTC scan failed" in caplog.text

    def test_set_dtc_query_wires_callable(self, tm):
        fn = MagicMock()
        tm.set_dtc_query(fn)
        assert tm._dtc_query_fn is fn
