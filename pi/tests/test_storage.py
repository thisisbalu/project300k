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

    def test_verify_integrity_false_skips_check_and_quarantine(self, tmp_path):
        from config import config
        from storage import get_connection

        db_path = str(tmp_path / "obd.db")
        open(db_path, "w").close()

        executed = []
        original_connect = sqlite3.connect

        def patched_connect(path, **kwargs):
            conn = MagicMock()
            conn.row_factory = None

            def execute(sql, *a, **k):
                executed.append(sql)
                return MagicMock(**{"fetchone.return_value": ("wal",)})

            conn.execute = execute
            return conn

        with patch("storage.sqlite3.connect", side_effect=patched_connect), \
             patch.object(config, "DB_PATH", db_path):
            conn = get_connection(verify_integrity=False)

        # No integrity_check ran, and nothing was quarantined.
        assert not any("integrity_check" in s for s in executed)
        assert not os.path.exists(db_path + ".corrupt")

    def test_corrupt_db_moves_wal_and_shm_sidecars(self, tmp_path, caplog):
        # The orphaned -wal would otherwise be replayed into the fresh DB,
        # re-introducing corruption and crash-looping recovery.
        import logging
        from config import config
        from storage import get_connection

        db_path = str(tmp_path / "obd.db")
        open(db_path, "w").close()
        open(db_path + "-wal", "w").close()
        open(db_path + "-shm", "w").close()

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
            return original_connect(path, **kwargs)

        with patch("storage.sqlite3.connect", side_effect=patched_connect), \
             patch.object(config, "DB_PATH", db_path), \
             caplog.at_level(logging.ERROR, logger="obd-collector"):
            conn = get_connection()
            conn.close()

        # Original sidecars quarantined before the fresh DB opened, so the
        # orphaned -wal can't be replayed into the new database. (The fresh
        # connection then creates its own new -wal/-shm, which is expected.)
        assert os.path.exists(db_path + ".corrupt-wal")
        assert os.path.exists(db_path + ".corrupt-shm")


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
    @staticmethod
    def _writer(conn):
        """Minimal QueueWriter stand-in exposing direct_query over a real conn."""
        from types import SimpleNamespace
        return SimpleNamespace(
            direct_query=lambda sql, params=(): conn.execute(sql, params).fetchone()
        )

    def test_returns_1_when_no_trips(self, db_conn):
        from storage import get_trip_number
        assert get_trip_number(self._writer(db_conn)) == 1

    def test_returns_n_plus_1_when_trips_exist(self, db_conn, trip_row):
        from storage import get_trip_number
        assert get_trip_number(self._writer(db_conn)) == 2

    def test_handles_sqlite_error_returns_0(self, caplog):
        import logging
        from storage import get_trip_number
        bad_writer = MagicMock()
        bad_writer.direct_query.side_effect = sqlite3.Error("disk full")
        with caplog.at_level(logging.WARNING, logger="obd-collector"):
            result = get_trip_number(bad_writer)
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


# ---------------------------------------------------------------------------
# repair_orphaned_trips()
# ---------------------------------------------------------------------------

class TestRepairOrphanedTrips:
    @staticmethod
    def _insert_trip(conn, trip_id, trip_number, start_time, end_time=None):
        conn.execute(
            "INSERT INTO trips (id, trip_number, start_time, end_time, synced) "
            "VALUES (?, ?, ?, ?, 0)",
            (trip_id, trip_number, start_time, end_time),
        )
        conn.commit()

    @staticmethod
    def _insert_reading(conn, table, trip_id, timestamp):
        import uuid
        conn.execute(
            f"INSERT INTO {table} (id, trip_id, timestamp, synced) VALUES (?, ?, ?, 0)",
            (str(uuid.uuid4()), trip_id, timestamp),
        )
        conn.commit()

    def test_returns_zero_when_no_open_trips(self, db_conn, trip_row):
        from storage import repair_orphaned_trips
        # trip_row has no end_time, so close it first to leave nothing open.
        db_conn.execute(
            "UPDATE trips SET end_time='2026-01-01T01:00:00+00:00' WHERE id=?",
            (trip_row,),
        )
        db_conn.commit()
        assert repair_orphaned_trips(db_conn) == 0

    def test_closes_trip_using_last_reading_timestamp(self, db_conn):
        from storage import repair_orphaned_trips
        tid = "trip-aaa"
        self._insert_trip(db_conn, tid, 1, "2026-01-01T00:00:00+00:00")
        self._insert_reading(db_conn, "obd_1s", tid, "2026-01-01T00:10:00+00:00")
        self._insert_reading(db_conn, "obd_1s", tid, "2026-01-01T00:30:00+00:00")
        self._insert_reading(db_conn, "obd_5s", tid, "2026-01-01T00:20:00+00:00")

        assert repair_orphaned_trips(db_conn) == 1

        row = db_conn.execute(
            "SELECT end_time, duration_s, synced FROM trips WHERE id=?", (tid,)
        ).fetchone()
        # Latest reading across all tables is the 00:30:00 obd_1s row.
        assert row["end_time"] == "2026-01-01T00:30:00+00:00"
        assert abs(row["duration_s"] - 1800) <= 1
        assert row["synced"] == 0

    def test_picks_latest_across_ford_tables(self, db_conn):
        from storage import repair_orphaned_trips
        tid = "trip-ford"
        self._insert_trip(db_conn, tid, 1, "2026-01-01T00:00:00+00:00")
        self._insert_reading(db_conn, "obd_1s", tid, "2026-01-01T00:05:00+00:00")
        self._insert_reading(db_conn, "ford_obd_20s", tid, "2026-01-01T00:40:00+00:00")

        repair_orphaned_trips(db_conn)

        row = db_conn.execute("SELECT end_time FROM trips WHERE id=?", (tid,)).fetchone()
        assert row["end_time"] == "2026-01-01T00:40:00+00:00"

    def test_trip_with_no_readings_closed_at_start_time(self, db_conn):
        from storage import repair_orphaned_trips
        tid = "trip-empty"
        self._insert_trip(db_conn, tid, 1, "2026-01-01T00:00:00+00:00")

        assert repair_orphaned_trips(db_conn) == 1

        row = db_conn.execute(
            "SELECT end_time, duration_s FROM trips WHERE id=?", (tid,)
        ).fetchone()
        assert row["end_time"] == "2026-01-01T00:00:00+00:00"
        assert row["duration_s"] == 0

    def test_does_not_touch_already_closed_trips(self, db_conn):
        from storage import repair_orphaned_trips
        closed = "trip-closed"
        self._insert_trip(
            db_conn, closed, 1,
            "2026-01-01T00:00:00+00:00", "2026-01-01T00:30:00+00:00",
        )
        db_conn.execute("UPDATE trips SET synced=1 WHERE id=?", (closed,))
        db_conn.commit()

        assert repair_orphaned_trips(db_conn) == 0

        row = db_conn.execute(
            "SELECT end_time, synced FROM trips WHERE id=?", (closed,)
        ).fetchone()
        # Untouched — end_time unchanged and synced flag not reset.
        assert row["end_time"] == "2026-01-01T00:30:00+00:00"
        assert row["synced"] == 1

    def test_repairs_multiple_open_trips(self, db_conn):
        from storage import repair_orphaned_trips
        self._insert_trip(db_conn, "t1", 1, "2026-01-01T00:00:00+00:00")
        self._insert_trip(db_conn, "t2", 2, "2026-01-02T00:00:00+00:00")
        self._insert_reading(db_conn, "obd_1s", "t1", "2026-01-01T00:15:00+00:00")
        self._insert_reading(db_conn, "obd_1s", "t2", "2026-01-02T00:25:00+00:00")

        assert repair_orphaned_trips(db_conn) == 2

        open_count = db_conn.execute(
            "SELECT COUNT(*) FROM trips WHERE end_time IS NULL"
        ).fetchone()[0]
        assert open_count == 0
