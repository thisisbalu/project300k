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
    2. Checks trip_id — skips enqueue if no active trip (avoids NOT NULL violation)
    3. Extracts the magnitude value (None if response is null/error)
    4. Builds a row dict: {id (UUID), trip_id, timestamp, <column>: value}
    5. Calls queue_writer.enqueue(table, row)

NULL values are stored honestly — a spike in NULLs for a specific PID
is itself a diagnostic signal (BT glitch, wrong Mode 22 address, sensor fault).

Note on obd.Async initialisation:
    obd.Async is a subclass of obd.OBD and must be instantiated with a port
    string — not by wrapping an existing obd.OBD connection object. Collector
    therefore opens its own async connection independently of OBDConnection,
    using the same port and settings.
"""

import uuid
from datetime import datetime, timezone

import obd

from config import config
from logger import logger
from obd_commands import ALL_PIDS, PIDConfig

ALLOWED_TABLES = {
    "obd_1s", "obd_5s", "obd_30s",
    "ford_obd_5s", "ford_obd_10s", "ford_obd_20s",
    "dtc_events", "pi_health_log", "trips",
}


class Collector:
    """Registers and manages all OBD async PID watchers.

    Opens its own obd.Async connection using the configured OBD port.
    Delegates trip context (trip_id) to TripManager and row persistence
    to QueueWriter.

    Attributes:
        _async_conn: python-obd Async connection managing the polling loop.
    """

    def __init__(self, queue_writer, trip_manager) -> None:
        """Initialise with required dependencies.

        Note: Collector no longer depends on OBDConnection because
        obd.Async must be instantiated with a port string directly,
        not wrapped around an existing obd.OBD instance.

        Args:
            queue_writer:  QueueWriter for thread-safe SQLite writes.
            trip_manager:  TripManager providing current trip_id and
                           receiving RPM/voltage updates for trip detection.
        """
        self._queue_writer = queue_writer
        self._trip_manager = trip_manager
        self._async_conn: obd.Async | None = None

    def start(self) -> None:
        """Open the async OBD connection and register all PID watchers.

        Creates an obd.Async connection using the same port and flags as
        OBDConnection. Iterates ALL_PIDS from obd_commands.py and registers
        one watcher per PID. Also registers RPM and voltage watchers for
        TripManager trip boundary detection.

        fast=False is required on Pi — without it, python-obd sends an AT
        command that causes the Pi Bluetooth stack to drop the connection.
        """
        # obd.Async is a subclass of obd.OBD — instantiate with port string,
        # not by wrapping an existing connection object.
        self._async_conn = obd.Async(config.OBD_PORT, fast=False, timeout=30)

        for pid in ALL_PIDS:
            self._async_conn.watch(
                pid.command,
                callback=self._make_callback(pid),
                # force=True registers the watcher even if the ECU reports
                # the PID as unsupported — some ECUs lie about supported PIDs.
                force=True,
            )
            logger.info(f"Watching PID: {pid.command.name} → {pid.table}.{pid.column}")

        # Trip detection watchers — feed TripManager with RPM and voltage
        # so it can detect trip start/end boundaries independently of data
        # collection callbacks.
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

        Skips enqueue when no trip is active (current_trip_id is None) to
        avoid inserting rows that would violate the trip_id NOT NULL constraint.

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
            # Skip rows when no trip is active — inserting trip_id=NULL
            # would violate the NOT NULL constraint on all OBD tables.
            trip_id = self._trip_manager.current_trip_id
            if trip_id is None:
                return

            # Extract raw magnitude — None if response has no value.
            # Storing None as NULL is intentional: a NULL spike for a specific
            # PID is a diagnostic signal (sensor fault, BT glitch, wrong address).
            value = None
            if response is not None and not response.is_null():
                value = (
                    response.value.magnitude
                    if hasattr(response.value, "magnitude")
                    else response.value
                )

            row = {
                "id": str(uuid.uuid4()),
                "trip_id": trip_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                pid.column: value,
                "synced": 0,
            }
            self._queue_writer.enqueue(pid.table, row)

        return callback
