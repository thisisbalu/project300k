"""
collector.py — Async OBD PID watcher registration and callback handling.

Sets up python-obd's async polling loop with one watcher per PID group.
Each watcher fires its callback at the configured interval. The callback
builds a row dict and hands it to QueueWriter — it never writes to SQLite
directly, ensuring all database access is serialised through the queue.

Polling tiers registered:
    Standard 1s   — RPM, speed, throttle, load
    Standard 5s   — coolant, oil temp, MAF, fuel trims, O2 sensors
    Standard 30s  — battery voltage, fuel level
    Ford 5s/10s/20s — Mode 22 enhanced PIDs (after FORScan confirmation)

Each callback:
    1. Receives an OBDResponse from python-obd
    2. Extracts the value (None if response is null/error)
    3. Builds a row dict: {id (UUID), trip_id, timestamp, <column>: value}
    4. Calls queue_writer.enqueue(table, row)

NULL values are stored honestly — a spike in NULLs for a specific PID
is itself a diagnostic signal (BT glitch, wrong Mode 22 address, sensor fault).
"""

from logger import logger


class Collector:
    """Registers and manages all OBD async PID watchers.

    Delegates trip context (trip_id) to TripManager and row persistence
    to QueueWriter. Does not hold any state beyond the active watchers.

    Args:
        obd_connection: OBDConnection instance with an active connection.
        queue_writer:   QueueWriter instance for thread-safe SQLite writes.
        trip_manager:   TripManager instance providing the current trip_id.
    """

    def __init__(self, obd_connection, queue_writer, trip_manager) -> None:
        self._obd = obd_connection
        self._queue_writer = queue_writer
        self._trip_manager = trip_manager

    def start(self) -> None:
        """Register all PID watchers and start the async polling loop."""
        # TODO: Task 9 — register standard + Ford Mode 22 watchers
        pass

    def stop(self) -> None:
        """Stop all watchers and shut down the async polling loop."""
        # TODO: Task 9 — stop watchers cleanly
        pass

    def _make_callback(self, table: str, column: str):
        """Return a closure that enqueues a single-column OBD reading.

        The closure captures table and column names so the same factory
        can produce callbacks for every PID without repetition.

        Args:
            table:  Target SQLite table (e.g. "obd_1s").
            column: Column name for the PID value (e.g. "rpm").

        Returns:
            Callable accepting an OBDResponse, enqueuing the row.
        """
        # TODO: Task 9 — return callback with UUID + trip_id + timestamp
        pass
