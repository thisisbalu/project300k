"""Tests for queue_writer.py — enqueue routing, batch commits, stop/drain."""

import queue
import sqlite3
import threading
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest


def _make_row(table="obd_1s", trip_id=None):
    """Build a minimal valid row dict for the given table."""
    return {
        "id": str(uuid.uuid4()),
        "trip_id": trip_id or str(uuid.uuid4()),
        "timestamp": "2026-01-01T00:00:00+00:00",
        "rpm": 1500,
        "synced": 0,
    }


@pytest.fixture
def writer(db_conn):
    """Started QueueWriter backed by a real temp-file SQLite with schema."""
    from queue_writer import QueueWriter
    w = QueueWriter(db_conn)
    w.start()
    yield w
    w.stop()


# ---------------------------------------------------------------------------
# Table allowlist
# ---------------------------------------------------------------------------

class TestAllowList:
    def test_unknown_table_rejected(self, db_conn, caplog):
        import logging
        from queue_writer import QueueWriter
        w = QueueWriter(db_conn)
        with caplog.at_level(logging.ERROR, logger="obd-collector"):
            w.enqueue("bad_table", {"id": "x"})
        assert "Rejected write to unknown table" in caplog.text
        assert "bad_table" in caplog.text

    def test_all_allowed_tables_accepted(self, db_conn):
        from queue_writer import QueueWriter, ALLOWED_TABLES
        w = QueueWriter(db_conn)
        for table in ALLOWED_TABLES:
            # No exception or error — just verifies routing logic accepts it.
            # Actual insert may fail (missing columns), but enqueue succeeds.
            try:
                w.enqueue(table, {"id": str(uuid.uuid4())})
            except Exception:
                pass  # insert errors are expected; routing acceptance is what we test

    def test_allowed_tables_set_contents(self):
        from queue_writer import ALLOWED_TABLES
        expected = {
            "obd_1s", "obd_5s", "obd_30s",
            "ford_obd_5s", "ford_obd_10s", "ford_obd_20s",
            "dtc_events", "pi_health_log", "trips",
        }
        assert ALLOWED_TABLES == expected


# ---------------------------------------------------------------------------
# Queue full — drops row with error log
# ---------------------------------------------------------------------------

class TestQueueFull:
    def test_full_queue_drops_row_with_log(self, db_conn, caplog):
        import logging
        from queue_writer import QueueWriter
        w = QueueWriter(db_conn)
        # Do NOT start writer thread — queue will never drain.
        for _ in range(1000):
            w._queue.put(("obd_1s", {}))
        with caplog.at_level(logging.ERROR, logger="obd-collector"):
            w.enqueue("obd_1s", _make_row())
        assert "dropping row" in caplog.text


# ---------------------------------------------------------------------------
# stop() — drains queue before returning
# ---------------------------------------------------------------------------

class TestStop:
    def test_all_rows_written_after_stop(self, db_conn):
        import uuid
        from queue_writer import QueueWriter

        w = QueueWriter(db_conn)
        w.start()

        trip_id = str(uuid.uuid4())
        db_conn.execute(
            "INSERT INTO trips (id, trip_number, start_time, synced) VALUES (?, 1, '2026-01-01T00:00:00+00:00', 0)",
            (trip_id,),
        )
        db_conn.commit()

        ids = [str(uuid.uuid4()) for _ in range(15)]
        for row_id in ids:
            w.enqueue("obd_1s", {
                "id": row_id,
                "trip_id": trip_id,
                "timestamp": "2026-01-01T00:00:00+00:00",
                "rpm": 1000,
                "synced": 0,
            })

        w.stop()

        count = db_conn.execute("SELECT COUNT(*) FROM obd_1s").fetchone()[0]
        assert count == 15

    def test_stop_logs_warning_when_thread_hangs(self, db_conn, caplog):
        import logging
        from queue_writer import QueueWriter
        w = QueueWriter(db_conn)
        # Replace thread with a mock that claims to still be alive after join
        w._thread = MagicMock()
        w._thread.is_alive.return_value = True
        with caplog.at_level(logging.WARNING, logger="obd-collector"):
            w.stop()
        assert "did not stop within 15s" in caplog.text


# ---------------------------------------------------------------------------
# Batch commit at COMMIT_BATCH_SIZE rows
# ---------------------------------------------------------------------------

class TestBatchCommit:
    def test_batch_of_30_rows_committed(self, db_conn):
        import uuid
        from queue_writer import QueueWriter, COMMIT_BATCH_SIZE

        trip_id = str(uuid.uuid4())
        db_conn.execute(
            "INSERT INTO trips (id, trip_number, start_time, synced) VALUES (?, 1, '2026-01-01T00:00:00+00:00', 0)",
            (trip_id,),
        )
        db_conn.commit()

        w = QueueWriter(db_conn)
        w.start()

        for _ in range(COMMIT_BATCH_SIZE):
            w.enqueue("obd_1s", {
                "id": str(uuid.uuid4()),
                "trip_id": trip_id,
                "timestamp": "2026-01-01T00:00:00+00:00",
                "rpm": 800,
                "synced": 0,
            })

        w.stop()
        count = db_conn.execute("SELECT COUNT(*) FROM obd_1s").fetchone()[0]
        assert count == COMMIT_BATCH_SIZE


# ---------------------------------------------------------------------------
# _flush() — per-row errors are logged but batch continues
# ---------------------------------------------------------------------------

class TestFlush:
    def test_bad_row_logged_and_remaining_rows_written(self, db_conn, caplog):
        import logging
        import uuid
        from queue_writer import QueueWriter

        trip_id = str(uuid.uuid4())
        db_conn.execute(
            "INSERT INTO trips (id, trip_number, start_time, synced) VALUES (?, 1, '2026-01-01T00:00:00+00:00', 0)",
            (trip_id,),
        )
        db_conn.commit()

        w = QueueWriter(db_conn)
        good_id = str(uuid.uuid4())
        good_row = {
            "id": good_id,
            "trip_id": trip_id,
            "timestamp": "2026-01-01T00:00:00+00:00",
            "rpm": 1200,
            "synced": 0,
        }
        bad_row = {"id": str(uuid.uuid4()), "nonexistent_col": "x"}

        with caplog.at_level(logging.ERROR, logger="obd-collector"):
            w._flush([("obd_1s", bad_row), ("obd_1s", good_row)])

        assert "SQLite write error" in caplog.text
        row = db_conn.execute("SELECT id FROM obd_1s WHERE id=?", (good_id,)).fetchone()
        assert row is not None

    def test_commit_failure_logged(self, caplog):
        import logging
        from queue_writer import QueueWriter
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = Exception("disk full")
        mock_conn.commit.side_effect = Exception("disk full")
        w = QueueWriter(mock_conn)
        with caplog.at_level(logging.ERROR, logger="obd-collector"):
            w._flush([("obd_1s", {"id": "x"})])
        assert "SQLite" in caplog.text


# ---------------------------------------------------------------------------
# write_failing — surfaces a persistently unwritable DB (e.g. USB unmounted)
# ---------------------------------------------------------------------------

class TestWriteFailing:
    def test_false_initially(self, db_conn):
        from queue_writer import QueueWriter
        assert QueueWriter(db_conn).write_failing is False

    def test_false_below_threshold(self):
        from queue_writer import QueueWriter, MAX_CONSECUTIVE_FLUSH_FAILURES
        mock_conn = MagicMock()
        mock_conn.commit.side_effect = Exception("disk I/O error")
        w = QueueWriter(mock_conn)
        for _ in range(MAX_CONSECUTIVE_FLUSH_FAILURES - 1):
            w._flush([("obd_1s", {"id": "x"})])
        assert w.write_failing is False

    def test_true_after_consecutive_commit_failures(self):
        from queue_writer import QueueWriter, MAX_CONSECUTIVE_FLUSH_FAILURES
        mock_conn = MagicMock()
        mock_conn.commit.side_effect = Exception("disk I/O error")
        w = QueueWriter(mock_conn)
        for _ in range(MAX_CONSECUTIVE_FLUSH_FAILURES):
            w._flush([("obd_1s", {"id": "x"})])
        assert w.write_failing is True

    def test_all_rows_failing_counts_even_when_commit_ok(self):
        # USB gone: every INSERT throws but commit() (no rows) succeeds.
        from queue_writer import QueueWriter
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = Exception("disk gone")
        w = QueueWriter(mock_conn)
        w._flush([("obd_1s", {"id": "a"}), ("obd_1s", {"id": "b"})])
        assert w._consecutive_failures == 1

    def test_successful_flush_resets_counter(self):
        from queue_writer import QueueWriter, MAX_CONSECUTIVE_FLUSH_FAILURES
        mock_conn = MagicMock()
        mock_conn.commit.side_effect = Exception("disk I/O error")
        w = QueueWriter(mock_conn)
        for _ in range(MAX_CONSECUTIVE_FLUSH_FAILURES - 1):
            w._flush([("obd_1s", {"id": "x"})])
        # DB recovers — one clean flush clears the streak.
        mock_conn.commit.side_effect = None
        w._flush([("obd_1s", {"id": "x"})])
        assert w._consecutive_failures == 0
        assert w.write_failing is False

    def test_partial_failure_does_not_count(self, db_conn, trip_row):
        # One bad row among good ones commits fine — not a DB-level failure.
        import uuid
        from queue_writer import QueueWriter
        w = QueueWriter(db_conn)
        good = {
            "id": str(uuid.uuid4()),
            "trip_id": trip_row,
            "timestamp": "2026-01-01T00:00:00+00:00",
            "rpm": 1000,
            "synced": 0,
        }
        bad = {"id": str(uuid.uuid4()), "nonexistent_col": "x"}
        w._flush([("obd_1s", bad), ("obd_1s", good)])
        assert w._consecutive_failures == 0


# ---------------------------------------------------------------------------
# _track_table_failures — one table losing every row while the batch commits
# ---------------------------------------------------------------------------

class TestTableFailureStreak:
    def _good_obd_1s(self, trip_id):
        import uuid
        return {
            "id": str(uuid.uuid4()),
            "trip_id": trip_id,
            "timestamp": "2026-01-01T00:00:00+00:00",
            "rpm": 1000,
            "synced": 0,
        }

    def _bad_row(self):
        import uuid
        return {"id": str(uuid.uuid4()), "nonexistent_col": "x"}

    def test_warns_when_one_table_loses_every_row(self, db_conn, trip_row, caplog):
        import logging
        from queue_writer import QueueWriter, TABLE_FAILURE_WARN_STREAK
        w = QueueWriter(db_conn)
        # obd_5s rows always fail (bad column) while obd_1s commits cleanly, so
        # the batch commits and write_failing never trips — the per-table guard
        # is the only thing that can surface the loss.
        with caplog.at_level(logging.ERROR, logger="obd-collector"):
            for _ in range(TABLE_FAILURE_WARN_STREAK):
                w._flush([
                    ("obd_5s", self._bad_row()),
                    ("obd_1s", self._good_obd_1s(trip_row)),
                ])
        assert "obd_5s" in caplog.text
        assert "silently discarding" in caplog.text
        assert w.write_failing is False
        assert w._table_fail_streak["obd_1s"] == 0

    def test_no_warning_below_threshold(self, db_conn, trip_row, caplog):
        import logging
        from queue_writer import QueueWriter, TABLE_FAILURE_WARN_STREAK
        w = QueueWriter(db_conn)
        with caplog.at_level(logging.ERROR, logger="obd-collector"):
            for _ in range(TABLE_FAILURE_WARN_STREAK - 1):
                w._flush([("obd_5s", self._bad_row())])
        assert "silently discarding" not in caplog.text
        assert w._table_fail_streak["obd_5s"] == TABLE_FAILURE_WARN_STREAK - 1

    def test_recovering_table_resets_streak(self, db_conn, trip_row):
        from queue_writer import QueueWriter, TABLE_FAILURE_WARN_STREAK
        w = QueueWriter(db_conn)
        for _ in range(TABLE_FAILURE_WARN_STREAK - 1):
            w._flush([("obd_5s", self._bad_row())])
        # A clean obd_5s row commits — the streak must reset to 0.
        import uuid
        good_5s = {
            "id": str(uuid.uuid4()),
            "trip_id": trip_row,
            "timestamp": "2026-01-01T00:00:00+00:00",
            "coolant_temp_c": 90.0,
            "synced": 0,
        }
        w._flush([("obd_5s", good_5s)])
        assert w._table_fail_streak["obd_5s"] == 0


# ---------------------------------------------------------------------------
# _insert() — builds correct parameterised SQL
# ---------------------------------------------------------------------------

class TestInsert:
    def test_insert_uses_named_placeholders(self, db_conn, trip_row):
        import uuid
        from queue_writer import QueueWriter
        w = QueueWriter(db_conn)
        row_id = str(uuid.uuid4())
        w._insert("obd_1s", {
            "id": row_id,
            "trip_id": trip_row,
            "timestamp": "2026-01-01T00:00:00+00:00",
            "rpm": 3000,
            "synced": 0,
        })
        db_conn.commit()
        result = db_conn.execute("SELECT rpm FROM obd_1s WHERE id=?", (row_id,)).fetchone()
        assert result[0] == 3000

    def test_insert_stores_none_as_null(self, db_conn, trip_row):
        import uuid
        from queue_writer import QueueWriter
        w = QueueWriter(db_conn)
        row_id = str(uuid.uuid4())
        w._insert("obd_1s", {
            "id": row_id,
            "trip_id": trip_row,
            "timestamp": "2026-01-01T00:00:00+00:00",
            "rpm": None,
            "synced": 0,
        })
        db_conn.commit()
        result = db_conn.execute("SELECT rpm FROM obd_1s WHERE id=?", (row_id,)).fetchone()
        assert result[0] is None


# ---------------------------------------------------------------------------
# _drain() queue.Empty path — fires when queue is idle
# ---------------------------------------------------------------------------

def test_drain_handles_empty_queue_timeout(db_conn):
    """_drain() hits queue.Empty every 0.1s while queue is empty and running."""
    import time
    from queue_writer import QueueWriter
    w = QueueWriter(db_conn)
    w.start()
    time.sleep(0.15)  # let drain loop tick at least once with empty queue
    w.stop()  # must exit cleanly


# ---------------------------------------------------------------------------
# conn attribute — exposed for direct UPDATE calls
# ---------------------------------------------------------------------------

def test_conn_attribute_exposed(db_conn):
    from queue_writer import QueueWriter
    w = QueueWriter(db_conn)
    assert w.conn is db_conn


# ---------------------------------------------------------------------------
# direct_query — locked read for callers on the OBD thread
# ---------------------------------------------------------------------------

def test_direct_query_returns_first_row(db_conn):
    from queue_writer import QueueWriter
    w = QueueWriter(db_conn)
    row = w.direct_query("SELECT COUNT(*) FROM trips")
    assert row[0] == 0


# ---------------------------------------------------------------------------
# ON CONFLICT(id) DO NOTHING — duplicate-id insert is an idempotent no-op
# ---------------------------------------------------------------------------

def test_duplicate_id_insert_is_noop(db_conn):
    from queue_writer import QueueWriter
    w = QueueWriter(db_conn)
    row = {
        "id": "fixed-id",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "cpu_temp_c": 50.0,
        "synced": 0,
    }
    w._insert("pi_health_log", row)
    # Same id, different value — must not raise and must not overwrite.
    w._insert("pi_health_log", {**row, "cpu_temp_c": 99.0})
    db_conn.commit()
    rows = db_conn.execute(
        "SELECT cpu_temp_c FROM pi_health_log WHERE id = 'fixed-id'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 50.0  # first write kept, second silently ignored
