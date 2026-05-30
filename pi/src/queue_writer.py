"""
queue_writer.py — Thread-safe bridge between OBD callbacks and SQLite.

python-obd async mode fires PID callbacks from a background thread. SQLite
writes must happen on a single thread to avoid contention. QueueWriter
solves this by acting as a producer/consumer buffer:

    OBD callback (background thread)
        └── enqueue(table, row)     — puts onto thread-safe queue.Queue

    QueueWriter._drain (dedicated writer thread)
        └── dequeues rows           — writes to SQLite one at a time

This means no two writers ever touch SQLite simultaneously, and no OBD
callback ever blocks waiting for a disk write to complete.

Commit strategy:
    Rows are batched and committed every COMMIT_BATCH_SIZE rows or every
    COMMIT_INTERVAL_S seconds, whichever comes first. Per-row commits at
    1Hz with 15 active PIDs = 15+ fsyncs/second to USB flash, which causes
    severe write amplification and premature drive wear. Batching reduces
    this to ~1 fsync every 2 seconds under normal polling load.

Thread safety for direct UPDATEs:
    trip.py._end_trip() must UPDATE the trips row (not INSERT a duplicate).
    That call arrives on the OBD callback thread, not the writer thread.
    _db_lock serialises all conn.execute() + conn.commit() calls so the
    callback-thread UPDATE and the writer-thread INSERT batch never race.

Usage:
    writer = QueueWriter(conn)
    writer.start()
    writer.enqueue("obd_1s", {"id": "...", "trip_id": "...", ...})
    writer.stop()  # drains remaining rows before returning
"""

from __future__ import annotations

import queue
import sqlite3
import threading
import time
from typing import Any

from logger import logger

# Commit after this many rows or after COMMIT_INTERVAL_S, whichever comes first.
COMMIT_BATCH_SIZE  = 30   # rows — at 15 PIDs/s this is ~2s of data per commit
COMMIT_INTERVAL_S  = 2.0  # seconds — maximum time between commits

# Reject writes to tables not in this set to catch routing bugs early.
ALLOWED_TABLES = {
    "obd_1s", "obd_5s", "obd_30s",
    "ford_obd_5s", "ford_obd_10s", "ford_obd_20s",
    "dtc_events", "pi_health_log", "trips",
}


class QueueWriter:
    """Serialises SQLite writes from multiple OBD callback threads.

    Attributes:
        conn:      SQLite connection — exposed for read-only callers that
                   need a connection reference (e.g. get_trip_number).
        _db_lock:  Mutex covering every conn.execute() + conn.commit() call.
                   Held by the writer thread during _flush() and by any
                   caller of direct_execute() on the OBD callback thread.
        _queue:    Bounded thread-safe FIFO queue of (table, row) tuples.
        _stop_event: Signals the drain loop to exit after flushing.
        _thread:   Daemon writer thread running _drain.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Initialise the writer with an open SQLite connection.

        Args:
            conn: sqlite3.Connection returned by storage.get_connection().
        """
        # Bounded queue — drops rows (with a log) if the writer thread stalls,
        # rather than growing without bound and causing OOM on 1GB Pi.
        self._queue: queue.Queue = queue.Queue(maxsize=1000)
        self._conn = conn
        # Expose conn for callers that need a read reference (e.g. get_trip_number).
        # All writes must go through enqueue() or direct_execute() — never
        # call conn.execute() directly from outside this class.
        self.conn = conn
        # Serialises all conn.execute() + conn.commit() calls regardless of thread.
        self._db_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._drain, daemon=True, name="queue-writer"
        )

    def start(self) -> None:
        """Start the background writer thread."""
        self._thread.start()
        logger.info("QueueWriter started")

    def stop(self) -> None:
        """Signal the writer to stop and wait for the queue to drain.

        Blocks until all enqueued rows have been written to SQLite.
        Call this during shutdown before closing the database connection.
        """
        self._stop_event.set()
        # 15s: SQLite lock wait is up to 10s, plus time to flush the final batch.
        self._thread.join(timeout=15)
        if self._thread.is_alive():
            logger.warning("QueueWriter thread did not stop within 15s — possible SQLite hang")
        logger.info("QueueWriter stopped")

    def enqueue(self, table: str, row: dict[str, Any]) -> None:
        """Put a row onto the write queue.

        Non-blocking. Safe to call from any thread including OBD callbacks.
        Drops the row and logs an error if the queue is full (writer stalled).

        Args:
            table: Target SQLite table name (must be in ALLOWED_TABLES).
            row:   Dict of column name → value. None values stored as NULL.
        """
        if table not in ALLOWED_TABLES:
            logger.error(f"Rejected write to unknown table: {table!r} — check routing")
            return

        try:
            self._queue.put_nowait((table, row))
        except queue.Full:
            # Writer thread is stalled — log and drop rather than block the
            # OBD callback thread or grow the queue unboundedly.
            logger.error(f"QueueWriter full — dropping row for table '{table}'")

    def direct_execute(self, sql: str, params: tuple = ()) -> None:
        """Execute a non-INSERT statement (UPDATE/DELETE) under the db lock.

        Used for trip end UPDATEs that arrive on the OBD callback thread and
        cannot go through the INSERT queue. Acquires _db_lock to prevent
        concurrent access with _flush() running on the writer thread.

        Args:
            sql:    Parameterised SQL statement with ? placeholders.
            params: Tuple of values to bind.
        """
        with self._db_lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def _drain(self) -> None:
        """Drain the queue and write rows to SQLite in batches.

        Accumulates rows up to COMMIT_BATCH_SIZE or COMMIT_INTERVAL_S,
        then commits once. This reduces USB flash fsyncs from ~15/s (per-row)
        to ~1 per COMMIT_INTERVAL_S under normal polling load.

        On stop signal, continues draining until the queue is fully empty
        and commits any remaining unflushed rows before returning.
        """
        pending: list[tuple[str, dict]] = []
        last_commit = time.monotonic()

        while True:
            should_stop = self._stop_event.is_set() and self._queue.empty()

            # Commit pending rows if batch is full, interval elapsed, or stopping.
            elapsed = time.monotonic() - last_commit
            if pending and (len(pending) >= COMMIT_BATCH_SIZE or elapsed >= COMMIT_INTERVAL_S or should_stop):
                self._flush(pending)
                pending.clear()
                last_commit = time.monotonic()

            if should_stop:
                break

            try:
                table, row = self._queue.get(timeout=0.1)
                pending.append((table, row))
                self._queue.task_done()
            except queue.Empty:
                continue

    def _flush(self, pending: list[tuple[str, dict]]) -> None:
        """Write all pending rows in a single transaction and commit once.

        Acquires _db_lock for the entire batch so direct_execute() calls
        from the OBD callback thread cannot interleave mid-batch.

        Args:
            pending: List of (table, row) tuples to write.
        """
        with self._db_lock:
            for table, row in pending:
                try:
                    self._insert(table, row)
                except Exception as e:
                    # Log and skip bad rows — one bad row must not prevent
                    # the rest of the batch from being committed.
                    logger.error(f"SQLite write error on '{table}': {e} — row discarded")
            try:
                self._conn.commit()
            except Exception as e:
                logger.error(f"SQLite commit failed: {e}")

    def _insert(self, table: str, row: dict[str, Any]) -> None:
        """Build and execute a parameterised INSERT for one row.

        Uses named placeholders (:key) to bind values safely — no string
        interpolation, no SQL injection risk from row data.
        Must be called with _db_lock held (via _flush).

        Args:
            table: Target SQLite table name (pre-validated by enqueue).
            row:   Dict of column name → value to insert.
        """
        columns      = ", ".join(row.keys())
        placeholders = ", ".join(f":{k}" for k in row.keys())
        sql = f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"
        self._conn.execute(sql, row)
