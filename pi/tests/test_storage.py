"""Tests for storage.py — connection config, schema, trip helpers."""

import os
import sqlite3
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _open_real_db(path):
    """Return a real SQLite connection with WAL + FK enabled."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _all_table_names(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def _all_index_names(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# get_connection()
# ---------------------------------------------------------------------------

class TestGetConnection:
    def test_returns_connection_with_wal_mode(self, tmp_path):
        from config import config
        from storage import get_connection

        db_path = str(tmp_path / "obd.db")
        with patch.object(config, "DB_PATH", db_path):
            conn = get_connection()
            try:
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                assert mode == "wal"
            finally:
                conn.close()

    def test_returns_connection_with_foreign_keys_on(self, tmp_path):
        from config import config
        from storage import get_connection

        db_path = str(tmp_path / "obd.db")
        with patch.object(config, "DB_PATH", db_path):
            conn = get_connection()
            try:
                fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
                assert fk == 1
            finally:
                conn.close()

    def test_non_wal_mode_logs_warning(self, tmp_path, caplog):
        import logging
        from config import config
        from storage import get_connection

        db_path = str(tmp_path / "obd.db")

        def _mock_exec(ret=None):
            m = MagicMock()
            if ret is not None:
                m.fetchone.return_value = ret
            return m

        mock_conn = MagicMock(spec=sqlite3.Connection)
        mock_conn.execute.side_effect = [
            _mock_exec(("memory",)),  # PRAGMA journal_mode=WAL → returns "memory"
            _mock_exec(),             # PRAGMA foreign_keys=ON
            _mock_exec(),             # PRAGMA synchronous=NORMAL
            _mock_exec(),             # PRAGMA cache_size=-8000
            _mock_exec(("ok",)),      # PRAGMA integrity_check
        ]

        with patch("storage.sqlite3.connect", return_value=mock_conn), \
             patch.object(config, "DB_PATH", db_path), \
             caplog.at_level(logging.WARNING, logger="obd-collector"):
            get_connection()

        assert "WAL mode not available" in caplog.text

    def test_corrupt_db_renamed_and_fresh_db_opened(self, tmp_path, caplog):
        import logging
        from config import config
        from storage import get_connection

        db_path = str(tmp_path / "obd.db")
        # Create the file so os.rename has something to rename
        open(db_path, "w").close()

        call_count = 0
        original_connect = sqlite3.connect

        def patched_connect(path, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                conn = MagicMock()
                conn.row_factory = None
                exec_iter = iter([
                    MagicMock(**{"fetchone.return_value": ("wal",)}),
                    MagicMock(),
                    MagicMock(),
                    MagicMock(),
                    MagicMock(**{"fetchone.return_value": ("corruption found",)}),
                ])
                conn.execute = lambda *a, **k: next(exec_iter)
                return conn
            # Second call: return a real clean connection
            return original_connect(path, **kwargs)

        with patch("storage.sqlite3.connect", side_effect=patched_connect), \
             patch.object(config, "DB_PATH", db_path), \
             caplog.at_level(logging.ERROR, logger="obd-collector"):
            conn = get_connection()
            conn.close()

        assert "integrity check failed" in caplog.text
        assert os.path.exists(db_path + ".corrupt")


# ---------------------------------------------------------------------------
# init_schema()
# ---------------------------------------------------------------------------

class TestInitSchema:
    EXPECTED_TABLES = {
        "schema_version", "trips", "obd_1s", "obd_5s", "obd_30s",
        "ford_obd_5s", "ford_obd_10s", "ford_obd_20s",
        "dtc_events", "pi_health_log",
    }

    EXPECTED_INDEXES = {
        "idx_obd_1s_trip_id", "idx_obd_1s_timestamp", "idx_obd_1s_synced",
        "idx_obd_5s_trip_id", "idx_obd_5s_timestamp", "idx_obd_5s_synced",
        "idx_obd_30s_trip_id", "idx_obd_30s_timestamp", "idx_obd_30s_synced",
        "idx_ford_obd_5s_trip_id", "idx_ford_obd_5s_timestamp", "idx_ford_obd_5s_synced",
        "idx_ford_obd_10s_trip_id", "idx_ford_obd_10s_timestamp", "idx_ford_obd_10s_synced",
        "idx_ford_obd_20s_trip_id", "idx_ford_obd_20s_timestamp", "idx_ford_obd_20s_synced",
        "idx_dtc_events_trip_id", "idx_dtc_events_timestamp",
        "idx_dtc_events_synced", "idx_dtc_events_code",
        "idx_trips_start_time", "idx_trips_synced",
        "idx_pi_health_log_timestamp", "idx_pi_health_log_synced",
    }

    def test_all_tables_created(self, tmp_path):
        from storage import init_schema
        conn = _open_real_db(tmp_path / "t.db")
        init_schema(conn)
        assert self.EXPECTED_TABLES <= _all_table_names(conn)
        conn.close()

    def test_all_indexes_created(self, tmp_path):
        from storage import init_schema
        conn = _open_real_db(tmp_path / "t.db")
        init_schema(conn)
        assert self.EXPECTED_INDEXES <= _all_index_names(conn)
        conn.close()

    def test_idempotent_double_call(self, tmp_path):
        from storage import init_schema
        conn = _open_real_db(tmp_path / "t.db")
        init_schema(conn)
        init_schema(conn)  # must not raise
        assert self.EXPECTED_TABLES <= _all_table_names(conn)
        conn.close()

    def test_schema_version_recorded(self, tmp_path):
        from storage import init_schema, SCHEMA_VERSION
        conn = _open_real_db(tmp_path / "t.db")
        init_schema(conn)
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        assert row[0] == SCHEMA_VERSION
        conn.close()

    def test_schema_version_not_duplicated_on_second_call(self, tmp_path):
        from storage import init_schema
        conn = _open_real_db(tmp_path / "t.db")
        init_schema(conn)
        init_schema(conn)
        count = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        assert count == 1
        conn.close()


# ---------------------------------------------------------------------------
# get_trip_number()
# ---------------------------------------------------------------------------

class TestGetTripNumber:
    def test_returns_1_when_no_trips(self, db_conn):
        from storage import get_trip_number
        assert get_trip_number(db_conn) == 1

    def test_returns_n_plus_1_when_trips_exist(self, db_conn, trip_row):
        from storage import get_trip_number
        assert get_trip_number(db_conn) == 2

    def test_handles_sqlite_error_returns_0(self, caplog):
        import logging
        from storage import get_trip_number
        bad_conn = MagicMock()
        bad_conn.execute.side_effect = sqlite3.Error("disk full")
        with caplog.at_level(logging.WARNING, logger="obd-collector"):
            result = get_trip_number(bad_conn)
        assert result == 0
        assert "Could not get trip number" in caplog.text


# ---------------------------------------------------------------------------
# update_trip_end()
# ---------------------------------------------------------------------------

class TestUpdateTripEnd:
    def test_writes_end_time_and_duration(self, db_conn, trip_row):
        from storage import update_trip_end
        from queue_writer import QueueWriter
        qw = QueueWriter(db_conn)
        end_time = "2026-01-01T01:00:00+00:00"
        update_trip_end(qw, trip_row, end_time)
        row = db_conn.execute("SELECT end_time, duration_s, synced FROM trips WHERE id=?", (trip_row,)).fetchone()
        assert row["end_time"] == end_time
        assert abs(row["duration_s"] - 3600) <= 1  # julianday() has float rounding
        assert row["synced"] == 0

    def test_handles_exception_without_raising(self, caplog):
        import logging
        from storage import update_trip_end
        bad_qw = MagicMock()
        bad_qw.direct_execute.side_effect = Exception("disk full")
        with caplog.at_level(logging.ERROR, logger="obd-collector"):
            update_trip_end(bad_qw, "fake-id", "2026-01-01T00:00:00+00:00")
        assert "Failed to write trip end" in caplog.text

    def test_sets_synced_to_zero(self, db_conn, trip_row):
        """Marking synced=0 ensures the updated row is picked up by the sync script."""
        from storage import update_trip_end
        from queue_writer import QueueWriter
        qw = QueueWriter(db_conn)
        # Manually mark synced=1 first
        db_conn.execute("UPDATE trips SET synced=1 WHERE id=?", (trip_row,))
        db_conn.commit()
        update_trip_end(qw, trip_row, "2026-01-01T01:00:00+00:00")
        row = db_conn.execute("SELECT synced FROM trips WHERE id=?", (trip_row,)).fetchone()
        assert row["synced"] == 0
