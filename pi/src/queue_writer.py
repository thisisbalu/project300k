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

# Consecutive fully-failed flushes before declaring the write path dead.
# At ~1 flush / COMMIT_INTERVAL_S this is ~10s of sustained write failure —
# well inside the 60s systemd watchdog window, leaving time for main to exit
# and systemd to restart a clean process (which re-runs get_connection()).
MAX_CONSECUTIVE_FLUSH_FAILURES = 5

# Consecutive flushes in which every row for a single table failed (while other
# tables committed fine) before logging a loud warning. The global failure
# counter above never trips in this case — the commit succeeds — so a column or
# routing bug would otherwise silently discard one table's stream forever. This
# surfaces it. At ~1 flush / COMMIT_INTERVAL_S this is ~10s of total loss.
TABLE_FAILURE_WARN_STREAK = 5

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
        # Count of consecutive flushes where the DB itself was unwritable.
        # Mutated only by the writer thread in _flush(); read by the main
        # thread via write_failing. Int read/write is atomic under the GIL.
        self._consecutive_failures = 0
        # Per-table streak of flushes where every row for that table failed to
        # insert while the batch still committed. Detects a single mis-routed or
        # schema-drifted table silently losing all its rows. Writer-thread only.
        self._table_fail_streak: dict[str, int] = {}
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

    @property
    def is_alive(self) -> bool:
        """True if the background writer thread is running.

        Read by the main watchdog loop: if this thread has died, SQLite writes
        have silently stopped, so the service must be restarted rather than left
        pinging the systemd watchdog as a zombie.
        """
        return self._thread.is_alive()

    @property
    def write_failing(self) -> bool:
        """True when recent flushes have all failed — the DB is likely unwritable.

        Read by the main watchdog loop. If the USB drive unmounts mid-session,
        every INSERT and commit throws but the drain thread stays alive and keeps
        pinging healthy while silently discarding rows. Surfacing this lets main
        exit so systemd restarts a clean process, which re-runs get_connection()
        and re-checks the mount/integrity.
        """
        return self._consecutive_failures >= MAX_CONSECUTIVE_FLUSH_FAILURES

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

    def direct_query(self, sql: str, params: tuple = ()) -> Any:
        """Run a read query under the db lock and return the first row.

        Used by callers on the OBD callback thread (e.g. get_trip_number) that
        must read the shared connection without racing _flush() on the writer
        thread. Acquiring _db_lock serialises the read against in-flight INSERT
        batches — a bare conn.execute() from another thread would not be safe.

        Args:
            sql:    Parameterised SELECT with ? placeholders.
            params: Tuple of values to bind.

        Returns:
            The first result row (tuple), or None if the query returned nothing.
        """
        with self._db_lock:
            return self._conn.execute(sql, params).fetchone()

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
            row_errors = 0
            # Per-table row and error tallies for silent-bleed detection.
            table_total: dict[str, int] = {}
            table_errors: dict[str, int] = {}
            for table, row in pending:
                table_total[table] = table_total.get(table, 0) + 1
                try:
                    self._insert(table, row)
                except Exception as e:
                    # Log and skip bad rows — one bad row must not prevent
                    # the rest of the batch from being committed.
                    row_errors += 1
                    table_errors[table] = table_errors.get(table, 0) + 1
                    logger.error(f"SQLite write error on '{table}': {e} — row discarded")
            try:
                self._conn.commit()
                commit_ok = True
            except Exception as e:
                commit_ok = False
                logger.error(f"SQLite commit failed: {e}")

        # A flush counts as failed only when the DB itself is unwritable — the
        # commit threw, or every row in a non-empty batch threw. A single bad
        # row among good ones still commits and resets the counter, so transient
        # row errors never trigger a restart; only sustained failure (e.g. USB
        # unmounted) drives _consecutive_failures up to the write_failing limit.
        if not commit_ok or (pending and row_errors == len(pending)):
            self._consecutive_failures += 1
        else:
            self._consecutive_failures = 0

        self._track_table_failures(table_total, table_errors, commit_ok)

    def _track_table_failures(
        self,
        table_total: dict[str, int],
        table_errors: dict[str, int],
        commit_ok: bool,
    ) -> None:
        """Warn when one table loses every row while the batch still commits.

        The global write_failing guard never trips here — the commit succeeded,
        so the DB is writable — but a column/routing bug specific to one table
        discards all its rows on every flush. Track a per-table streak and log a
        loud warning once it crosses TABLE_FAILURE_WARN_STREAK so the silent loss
        becomes visible (in logs and, via last_error, in the health snapshot).

        Only meaningful when the commit succeeded; a failed commit is the global
        path and resetting streaks there would mask a recovering table.
        """
        if not commit_ok:
            return
        for table, total in table_total.items():
            if table_errors.get(table, 0) == total:
                streak = self._table_fail_streak.get(table, 0) + 1
                self._table_fail_streak[table] = streak
                if streak == TABLE_FAILURE_WARN_STREAK:
                    logger.error(
                        f"Every row for table '{table}' has failed to insert for "
                        f"{streak} consecutive flushes while other tables commit — "
                        "likely a column/routing bug silently discarding all "
                        f"'{table}' rows. Check the schema matches the row shape."
                    )
            else:
                self._table_fail_streak[table] = 0

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
        # ON CONFLICT(id) DO NOTHING — a re-enqueued row (same UUID) becomes an
        # idempotent no-op rather than an IntegrityError, matching the server's
        # dedup contract. Every table has id TEXT PRIMARY KEY.
        sql = (
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO NOTHING"
        )
        self._conn.execute(sql, row)
