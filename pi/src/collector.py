"""
collector.py — Async OBD PID watcher registration and callback handling.

Sets up python-obd's async polling loop with one watcher per PID. Each
watcher fires its callback at the configured interval. The callback builds
a row dict and hands it to QueueWriter — it never writes to SQLite directly,
ensuring all database access is serialised through the queue.

All PIDs are registered from ALL_PIDS in obd_commands.py, so adding or
removing a PID requires no changes here — only in obd_commands.py.

Each callback:
    1. Receives an OBDResponse from python-obd
    2. Extracts the magnitude value (None if response is null/error)
    3. Builds a row dict: {id (UUID), trip_id, timestamp, <column>: value}
    4. Calls queue_writer.enqueue(table, row)

NULL values are stored honestly — a spike in NULLs for a specific PID
is itself a diagnostic signal (BT glitch, wrong Mode 22 address, sensor fault).
"""

import uuid
from datetime import datetime, timezone

import obd

from logger import logger
from obd_commands import ALL_PIDS, PIDConfig


class Collector:
    """Registers and manages all OBD async PID watchers.

    Delegates trip context (trip_id) to TripManager and row persistence
    to QueueWriter. Does not hold any state beyond the async connection.

    Attributes:
        _async_conn: python-obd async connection used for watcher registration.
    """

    def __init__(self, obd_connection, queue_writer, trip_manager) -> None:
        """Initialise with all required dependencies.

        Args:
            obd_connection: OBDConnection instance — provides the underlying
                            obd.OBD connection for creating the async wrapper.
            queue_writer:   QueueWriter for thread-safe SQLite writes.
            trip_manager:   TripManager providing the current trip_id and
                            receiving RPM/voltage updates for trip detection.
        """
        self._obd = obd_connection
        self._queue_writer = queue_writer
        self._trip_manager = trip_manager
        self._async_conn: obd.Async | None = None

    def start(self) -> None:
        """Open the async connection and register all PID watchers.

        Iterates ALL_PIDS from obd_commands.py and registers one watcher
        per PID. Also registers RPM and battery voltage watchers for
        TripManager so it receives updates for trip boundary detection.
        """
        self._async_conn = obd.Async(self._obd.connection)

        # Register all data collection PIDs from obd_commands.ALL_PIDS.
        # Each PIDConfig carries everything needed: command, table, column, interval.
        for pid in ALL_PIDS:
            self._async_conn.watch(
                pid.command,
                callback=self._make_callback(pid),
                force=True,
            )
            logger.info(f"Watching PID: {pid.command.name} → {pid.table}.{pid.column}")

        # Register trip detection callbacks separately — these feed TripManager
        # which needs RPM and voltage to detect trip start/end boundaries.
        # force=True ensures the watcher is registered even if the ECU reports
        # the PID as unsupported (some ECUs lie about supported PIDs).
        self._async_conn.watch(
            obd.commands.RPM,
            callback=self._trip_manager.on_rpm,
            force=True,
        )
        self._async_conn.watch(
            obd.commands.CONTROL_MODULE_VOLTAGE,
            callback=self._trip_manager.on_voltage,
            force=True,
        )

        self._async_conn.start()
        logger.info(f"Collector started — {len(ALL_PIDS)} PIDs active")

    def stop(self) -> None:
        """Stop all watchers and close the async polling loop."""
        if self._async_conn is not None:
            self._async_conn.stop()
            self._async_conn = None
            logger.info("Collector stopped")

    def _make_callback(self, pid: PIDConfig):
        """Return a closure that enqueues one OBD reading to SQLite.

        The closure captures the PIDConfig so the same factory produces
        correctly routed callbacks for every PID without repetition.

        The value extracted from the OBDResponse is the raw magnitude
        (float/int) without units — units are encoded in the column name
        (e.g. coolant_temp_c, battery_v). None is stored as NULL when the
        response is empty or the ECU returned an error.

        Args:
            pid: PIDConfig carrying table, column, and interval for this PID.

        Returns:
            Callable that accepts an OBDResponse and enqueues the row.
        """
        def callback(response: obd.OBDResponse) -> None:
            # Extract raw magnitude — None if response has no value.
            # Storing None as NULL is intentional: a NULL spike for a specific
            # PID is a diagnostic signal (sensor fault, BT glitch, wrong address).
            value = None
            if response is not None and not response.is_null():
                value = response.value.magnitude if hasattr(response.value, "magnitude") else response.value

            row = {
                "id": str(uuid.uuid4()),
                "trip_id": self._trip_manager.current_trip_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                pid.column: value,
                "synced": 0,
            }
            self._queue_writer.enqueue(pid.table, row)

        return callback
