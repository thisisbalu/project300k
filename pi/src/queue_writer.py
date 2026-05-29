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

Usage:
    writer = QueueWriter(conn)
    writer.start()
    writer.enqueue("obd_1s", {"id": "...", "trip_id": "...", ...})
    writer.stop()  # drains remaining rows before returning
"""

import queue
import sqlite3
import threading
from typing import Any

from logger import logger


class QueueWriter:
    """Serialises SQLite writes from multiple OBD callback threads.

    Attributes:
        _queue:       Unbounded thread-safe FIFO queue of (table, row) tuples.
        _conn:        SQLite connection — only accessed from _drain thread.
        _stop_event:  Signals the drain loop to exit after flushing.
        _thread:      Daemon writer thread running _drain.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Initialise the writer with an open SQLite connection.

        Args:
            conn: sqlite3.Connection returned by storage.get_connection().
        """
        self._queue: queue.Queue = queue.Queue()
        self._conn = conn
        # Expose conn publicly so storage functions (e.g. update_trip_end)
        # can issue direct UPDATE statements that cannot go through the
        # INSERT-only queue path.
        self.conn = conn
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
        self._thread.join()
        logger.info("QueueWriter stopped")

    def enqueue(self, table: str, row: dict[str, Any]) -> None:
        """Put a row onto the write queue.

        Non-blocking. Safe to call from any thread including OBD callbacks.

        Args:
            table: Target SQLite table name (e.g. "obd_1s").
            row:   Dict of column name → value. None values are stored as NULL.
        """
        self._queue.put((table, row))

    def _drain(self) -> None:
        """Drain the queue and write rows to SQLite.

        Runs on the dedicated writer thread. Processes rows one at a time
        using a 100ms timeout on each dequeue so the stop signal is checked
        regularly without busy-waiting.

        On stop signal, continues draining until the queue is fully empty
        before returning — ensures no in-flight rows are lost on clean shutdown.

        Write errors are logged and discarded rather than crashing the thread.
        A single bad row must not stop all subsequent writes.
        """
        while True:
            # Check stop signal only after the queue is empty to ensure
            # all enqueued rows are persisted before the thread exits.
            if self._stop_event.is_set() and self._queue.empty():
                break

            try:
                # Block for up to 100ms waiting for a row. The short timeout
                # allows the stop condition above to be re-evaluated frequently
                # without spinning the CPU at 100%.
                table, row = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue

            try:
                self._write(table, row)
            except Exception as e:
                # Log and discard — one bad row must not halt all subsequent writes.
                logger.error(f"SQLite write error on table '{table}': {e} — row discarded")
            finally:
                self._queue.task_done()

    def _write(self, table: str, row: dict[str, Any]) -> None:
        """Build and execute a parameterised INSERT for one row.

        Constructs the SQL dynamically from the row dict keys. Uses
        named placeholders (:key) to safely bind values — no string
        interpolation, no SQL injection risk.

        Args:
            table: Target SQLite table name.
            row:   Dict of column name → value to insert.
        """
        columns = ", ".join(row.keys())
        placeholders = ", ".join(f":{k}" for k in row.keys())
        sql = f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"
        self._conn.execute(sql, row)
        self._conn.commit()
