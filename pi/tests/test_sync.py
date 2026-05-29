"""Tests for sync.py — network check, health snapshot, per-table batch sync."""

import sqlite3
import uuid
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# _check_network()
# ---------------------------------------------------------------------------

class TestCheckNetwork:
    def test_returns_false_when_no_ip_on_wlan0(self, caplog):
        import logging
        from sync import _check_network
        mock_result = MagicMock()
        mock_result.stdout = "link/ether aa:bb:cc:dd:ee:ff"  # no "inet "
        with patch("sync.subprocess.run", return_value=mock_result), \
             caplog.at_level(logging.INFO, logger="obd-collector"):
            assert _check_network() is False
        assert "no hotspot" in caplog.text

    def test_returns_false_when_ping_fails(self, caplog):
        import logging
        from sync import _check_network
        mock_ip_result = MagicMock()
        mock_ip_result.stdout = "inet 192.168.1.2/24"
        mock_ping_result = MagicMock()
        mock_ping_result.returncode = 1
        with patch("sync.subprocess.run", side_effect=[mock_ip_result, mock_ping_result]), \
             caplog.at_level(logging.INFO, logger="obd-collector"):
            assert _check_network() is False
        assert "unreachable" in caplog.text

    def test_returns_true_when_both_checks_pass(self):
        from sync import _check_network
        mock_ip_result = MagicMock()
        mock_ip_result.stdout = "inet 192.168.1.2/24"
        mock_ping_result = MagicMock()
        mock_ping_result.returncode = 0
        with patch("sync.subprocess.run", side_effect=[mock_ip_result, mock_ping_result]):
            assert _check_network() is True

    def test_wlan0_exception_returns_false(self, caplog):
        import logging
        from sync import _check_network
        with patch("sync.subprocess.run", side_effect=Exception("no interface")), \
             caplog.at_level(logging.WARNING, logger="obd-collector"):
            assert _check_network() is False
        assert "Network check failed (wlan0)" in caplog.text

    def test_ping_exception_returns_false(self, caplog):
        import logging
        from sync import _check_network
        mock_ip_result = MagicMock()
        mock_ip_result.stdout = "inet 192.168.1.2/24"
        with patch("sync.subprocess.run", side_effect=[mock_ip_result, Exception("ping error")]), \
             caplog.at_level(logging.WARNING, logger="obd-collector"):
            assert _check_network() is False
        assert "Network check failed (ping)" in caplog.text


# ---------------------------------------------------------------------------
# _sync_table()
# ---------------------------------------------------------------------------

class TestSyncTable:
    def _insert_obd_rows(self, conn, trip_id, count):
        for _ in range(count):
            conn.execute(
                "INSERT INTO obd_1s (id, trip_id, timestamp, rpm, synced) VALUES (?, ?, '2026-01-01T00:00:00+00:00', 1000, 0)",
                (str(uuid.uuid4()), trip_id),
            )
        conn.commit()

    def test_returns_count_on_success(self, db_conn, trip_row):
        from sync import _sync_table
        self._insert_obd_rows(db_conn, trip_row, 3)

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None

        with patch("sync.requests.post", return_value=mock_response):
            count = _sync_table(db_conn, "obd_1s")

        assert count == 3

    def test_marks_rows_synced_1_after_post(self, db_conn, trip_row):
        from sync import _sync_table
        self._insert_obd_rows(db_conn, trip_row, 2)

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None

        with patch("sync.requests.post", return_value=mock_response):
            _sync_table(db_conn, "obd_1s")

        unsynced = db_conn.execute("SELECT COUNT(*) FROM obd_1s WHERE synced=0").fetchone()[0]
        assert unsynced == 0

    def test_skips_nonexistent_table(self, db_conn, caplog):
        import logging
        from sync import _sync_table
        with caplog.at_level(logging.DEBUG, logger="obd-collector"):
            count = _sync_table(db_conn, "ford_obd_5s")
        assert count == 0
        assert "does not exist" in caplog.text

    def test_returns_zero_when_no_unsynced_rows(self, db_conn):
        from sync import _sync_table
        count = _sync_table(db_conn, "obd_1s")
        assert count == 0

    def test_request_exception_stops_sync_and_logs(self, db_conn, trip_row, caplog):
        import logging
        from sync import _sync_table
        self._insert_obd_rows(db_conn, trip_row, 2)

        import requests as _req
        with patch("sync.requests.post", side_effect=_req.RequestException("connection refused")), \
             caplog.at_level(logging.ERROR, logger="obd-collector"):
            count = _sync_table(db_conn, "obd_1s")

        assert count == 0
        assert "Sync POST failed" in caplog.text

    def test_rows_remain_unsynced_after_failure(self, db_conn, trip_row):
        from sync import _sync_table
        import requests as _req
        self._insert_obd_rows(db_conn, trip_row, 3)

        with patch("sync.requests.post", side_effect=_req.RequestException("timeout")):
            _sync_table(db_conn, "obd_1s")

        unsynced = db_conn.execute("SELECT COUNT(*) FROM obd_1s WHERE synced=0").fetchone()[0]
        assert unsynced == 3

    def test_batch_size_limits_rows_per_post(self, db_conn, trip_row):
        from sync import _sync_table
        from config import config
        self._insert_obd_rows(db_conn, trip_row, 10)

        captured_payloads = []

        def fake_post(url, json=None, headers=None, timeout=None):
            captured_payloads.append(json)
            r = MagicMock()
            r.raise_for_status.return_value = None
            return r

        with patch.object(config, "SYNC_BATCH_SIZE", 3), \
             patch("sync.requests.post", side_effect=fake_post):
            _sync_table(db_conn, "obd_1s")

        # With 10 rows and batch size 3, we expect 4 POST calls (3+3+3+1)
        assert len(captured_payloads) == 4
        for p in captured_payloads[:-1]:
            assert len(p["rows"]) == 3


# ---------------------------------------------------------------------------
# _write_health_snapshot()
# ---------------------------------------------------------------------------

class TestWriteHealthSnapshot:
    def test_inserts_row_into_pi_health_log(self, db_conn):
        from sync import _write_health_snapshot

        mock_metrics = {
            "cpu_temp_c": 45.0,
            "cpu_usage_pct": 10.0,
            "memory_free_mb": 512.0,
            "disk_free_mb": 10000.0,
            "uptime_s": 3600,
            "usb_drive_mounted": 1,
            "bt_adapter_present": 1,
            "obd_reconnect_count": 0,
            "restart_count": 2,
            "rtc_ok": 1,
            "last_error": None,
            "collector_version": "1.0.0",
        }

        with patch("sync.health.collect", return_value=mock_metrics):
            _write_health_snapshot(db_conn)

        count = db_conn.execute("SELECT COUNT(*) FROM pi_health_log").fetchone()[0]
        assert count == 1

    def test_snapshot_marked_unsynced(self, db_conn):
        from sync import _write_health_snapshot

        mock_metrics = {
            "cpu_temp_c": None, "cpu_usage_pct": 0.0, "memory_free_mb": 256.0,
            "disk_free_mb": None, "uptime_s": 60, "usb_drive_mounted": 0,
            "bt_adapter_present": 0, "obd_reconnect_count": 0,
            "restart_count": 1, "rtc_ok": 0, "last_error": None,
            "collector_version": "unknown",
        }

        with patch("sync.health.collect", return_value=mock_metrics):
            _write_health_snapshot(db_conn)

        row = db_conn.execute("SELECT synced FROM pi_health_log").fetchone()
        assert row[0] == 0

    def test_sqlite_error_logged_without_raising(self, db_conn, caplog):
        import logging
        from sync import _write_health_snapshot
        bad_conn = MagicMock()
        bad_conn.execute.return_value.fetchone.return_value = (0,)
        import sqlite3 as _sq
        bad_conn.execute.side_effect = [
            MagicMock(**{"fetchone.return_value": (0,)}),  # COUNT obd_1s
            MagicMock(**{"fetchone.return_value": (0,)}),  # COUNT obd_5s
            MagicMock(**{"fetchone.return_value": (0,)}),  # COUNT obd_30s
            _sq.OperationalError("no table ford"),          # Ford table missing (logged at debug)
            _sq.OperationalError("no table ford"),
            _sq.OperationalError("no table ford"),
            MagicMock(**{"fetchone.return_value": (0,)}),  # COUNT dtc_events
            MagicMock(**{"fetchone.return_value": (0,)}),  # COUNT pi_health_log
            MagicMock(**{"fetchone.return_value": (0,)}),  # COUNT trips
            _sq.Error("disk full"),                         # INSERT fails
        ]
        mock_metrics = {
            "cpu_temp_c": None, "cpu_usage_pct": 0.0, "memory_free_mb": 256.0,
            "disk_free_mb": None, "uptime_s": 60, "usb_drive_mounted": 0,
            "bt_adapter_present": 0, "obd_reconnect_count": 0,
            "restart_count": 0, "rtc_ok": 0, "last_error": None,
            "collector_version": "unknown",
        }
        with patch("sync.health.collect", return_value=mock_metrics), \
             caplog.at_level(logging.ERROR, logger="obd-collector"):
            _write_health_snapshot(bad_conn)
        assert "Failed to write health snapshot" in caplog.text

    def test_counts_unsynced_rows_across_tables(self, db_conn, trip_row):
        from sync import _write_health_snapshot

        db_conn.execute(
            "INSERT INTO obd_1s (id, trip_id, timestamp, synced) VALUES (?, ?, '2026-01-01T00:00:00+00:00', 0)",
            (str(uuid.uuid4()), trip_row),
        )
        db_conn.commit()

        captured = {}

        def fake_collect(**kwargs):
            return {
                "cpu_temp_c": None, "cpu_usage_pct": 0.0, "memory_free_mb": 256.0,
                "disk_free_mb": None, "uptime_s": 60, "usb_drive_mounted": 0,
                "bt_adapter_present": 0, "obd_reconnect_count": kwargs.get("obd_reconnect_count", 0),
                "restart_count": kwargs.get("restart_count", 0), "rtc_ok": 0,
                "last_error": None, "collector_version": "unknown",
            }

        rows_collected = []
        original_collect = fake_collect

        def patched_collect(**kwargs):
            result = original_collect(**kwargs)
            return result

        with patch("sync.health.collect", side_effect=patched_collect):
            _write_health_snapshot(db_conn)

        row = db_conn.execute("SELECT rows_collected FROM pi_health_log").fetchone()
        assert row[0] >= 1  # at least the obd_1s row we inserted


# ---------------------------------------------------------------------------
# run() — integration of network check + health + table sync
# ---------------------------------------------------------------------------

class TestRun:
    def test_run_skips_everything_when_network_fails(self, caplog):
        import logging
        from sync import run
        with patch("sync._check_network", return_value=False), \
             patch("sync.get_connection") as mock_conn, \
             caplog.at_level(logging.INFO, logger="obd-collector"):
            run()
        mock_conn.assert_not_called()

    def test_run_closes_connection_on_completion(self, db_conn):
        from sync import run
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        with patch("sync._check_network", return_value=True), \
             patch("sync.get_connection", return_value=mock_conn), \
             patch("sync._write_health_snapshot"), \
             patch("sync._sync_table", return_value=0):
            run()

        mock_conn.close.assert_called_once()

    def test_run_closes_connection_on_exception(self):
        from sync import run
        mock_conn = MagicMock()

        with patch("sync._check_network", return_value=True), \
             patch("sync.get_connection", return_value=mock_conn), \
             patch("sync._write_health_snapshot", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                run()

        mock_conn.close.assert_called_once()
