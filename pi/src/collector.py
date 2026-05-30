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
    3. Skips 1s/5s PIDs when trip_manager.is_paused (RPM=0 for >30s)
    4. Extracts the magnitude value (None if response is null/error)
    5. Builds a row dict: {id (UUID), trip_id, timestamp, <column>: value}
    6. Calls queue_writer.enqueue(table, row)

NULL values are stored honestly — a spike in NULLs for a specific PID
is itself a diagnostic signal (BT glitch, wrong Mode 22 address, sensor fault).

Duplicate watch() for RPM and CONTROL_MODULE_VOLTAGE:
    Both PIDs appear in ALL_PIDS (data collection) AND are watched again
    for TripManager callbacks. In python-obd 0.7.3, Async.watch() appends
    callbacks to a list per command rather than replacing — both callbacks
    fire from a single poll. This is the intended design.

Reconnect monitoring:
    A background monitor thread checks is_connected() every 10s. On drop,
    it stops the async loop, calls obd_connection.reconnect() (which
    retries until the dongle responds), then restarts the async loop.
    The same trip_id is preserved — the gap in obd_1s is honest.

Note on obd.Async initialisation:
    obd.Async is a subclass of obd.OBD and must be instantiated with a port
    string — not by wrapping an existing obd.OBD connection object. Collector
    therefore opens its own async connection independently of OBDConnection,
    using the same port and settings.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone

import obd

from config import config
from logger import logger
from obd_commands import ALL_PIDS, PIDConfig


class Collector:
    """Registers and manages all OBD async PID watchers.

    Opens its own obd.Async connection using the configured OBD port.
    Delegates trip context (trip_id) to TripManager and row persistence
    to QueueWriter. Monitors the connection health and reconnects on drop.

    Attributes:
        _async_conn:     python-obd Async connection managing the polling loop.
        _stop_event:     Signals the monitor thread to stop.
        _monitor_thread: Background thread checking connection health every 10s.
    """

    def __init__(self, queue_writer, trip_manager, obd_connection) -> None:
        """Initialise with required dependencies.

        Args:
            queue_writer:   QueueWriter for thread-safe SQLite writes.
            trip_manager:   TripManager providing current trip_id, is_paused,
                            and receiving RPM/voltage updates for trip detection.
            obd_connection: OBDConnection for reconnect() and reconnect_count.
        """
        self._queue_writer = queue_writer
        self._trip_manager = trip_manager
        self._obd_connection = obd_connection
        self._async_conn: obd.Async | None = None
        self._last_enqueue: dict[str, float] = {}
        self._stop_event = threading.Event()
        self._monitor_thread: threading.Thread | None = None

    def start(self) -> None:
        """Open the async OBD connection, register all PID watchers, start monitoring.

        Creates an obd.Async connection using the same port and flags as
        OBDConnection. Iterates ALL_PIDS from obd_commands.py and registers
        one watcher per PID. Also registers RPM and voltage watchers for
        TripManager trip boundary detection.

        fast=False is required on Pi — without it, python-obd sends an AT
        command that causes the Pi Bluetooth stack to drop the connection.
        """
        self._stop_event.clear()
        self._connect_and_watch()

        self._monitor_thread = threading.Thread(
            target=self._monitor_connection,
            daemon=True,
            name="obd-monitor",
        )
        self._monitor_thread.start()
        logger.info(f"Collector started — {len(ALL_PIDS)} PIDs active")

    def stop(self) -> None:
        """Stop the monitor thread, all watchers, and close the async polling loop."""
        self._stop_event.set()
        if self._async_conn is not None:
            self._async_conn.stop()
            self._async_conn = None
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=5)
        logger.info("Collector stopped")

    def _connect_and_watch(self) -> None:
        """Open obd.Async and register all PID watchers.

        Extracted so it can be called both at start() and during reconnect
        inside _monitor_connection() without duplicating registration logic.
        """
        # obd.Async is a subclass of obd.OBD — instantiate with port string,
        # not by wrapping an existing connection object.
        self._async_conn = obd.Async(config.OBD_PORT, fast=False, timeout=30)

        # Track last-enqueue time per PID for interval enforcement.
        # python-obd 0.7.3 does not support per-watcher polling intervals —
        # all watched PIDs are polled at the async loop's natural rate (~1Hz).
        # The time-filter in _make_callback() enforces the declared interval_s
        # by skipping enqueue if insufficient time has elapsed since last write.
        for pid in ALL_PIDS:
            self._last_enqueue.setdefault(pid.command.name, 0.0)
            self._async_conn.watch(
                pid.command,
                callback=self._make_callback(pid),
                # force=True registers the watcher even if the ECU reports
                # the PID as unsupported — some ECUs lie about supported PIDs.
                force=True,
            )
            logger.info(f"Watching PID: {pid.command.name} → {pid.table}.{pid.column} every {pid.interval_s}s")

        # Trip detection watchers — feed TripManager with RPM and voltage
        # so it can detect trip start/end boundaries independently of data
        # collection callbacks.
        # python-obd 0.7.3 Async.watch() APPENDS callbacks per command rather
        # than replacing them, so both the data-collection callback (registered
        # above for RPM/CONTROL_MODULE_VOLTAGE in ALL_PIDS) and these TripManager
        # callbacks will fire from the same single poll.
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

    def _monitor_connection(self) -> None:
        """Monitor the async OBD connection and reconnect on drop.

        Checks is_connected() every 10s. On drop, stops the async loop,
        calls obd_connection.reconnect() (which retries until the dongle
        responds), then restarts the async loop with all watchers re-registered.

        The current trip_id is preserved across reconnects — the gap in
        obd_1s data is honest and acceptable.
        """
        # _stop_event.wait(10) returns True when the event is set (stop requested)
        # and False when the 10s timeout elapses — cleaner than time.sleep(10).
        while not self._stop_event.wait(timeout=10):
            if self._async_conn is None:
                break
            if not self._async_conn.is_connected():
                logger.warning("Async OBD connection dropped — reconnecting")
                try:
                    self._async_conn.stop()
                    self._async_conn = None
                    # reconnect() increments reconnect_count and writes it to
                    # disk so the sync script can include the live count.
                    self._obd_connection.reconnect()
                    self._connect_and_watch()
                    logger.info("Async OBD connection restored")
                except Exception as e:
                    logger.error(f"Async OBD reconnect failed: {e}")

    def _make_callback(self, pid: PIDConfig):
        """Return a closure that enqueues one OBD reading to SQLite.

        The closure captures the PIDConfig so the same factory produces
        correctly routed callbacks for every PID without repetition.

        Skips enqueue when no trip is active (current_trip_id is None) to
        avoid inserting rows that would violate the trip_id NOT NULL constraint.

        Skips 1s and 5s PIDs when polling is paused (RPM=0 for >30s) —
        implements the architecture's intent of suppressing fast-tier data
        while the engine is off. The 30s tier (battery_v, fuel level) is
        unaffected and continues to run for resting health data.

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

            # Suppress 1s and 5s PIDs when RPM has been 0 for >30s.
            # The 30s tier (battery_v, fuel level, ambient temp) is not
            # suppressed — those slow-moving signals are useful at rest.
            if self._trip_manager.is_paused and pid.interval_s < 30:
                return

            # Enforce polling interval — python-obd fires callbacks at the
            # async loop rate (~1Hz) regardless of PID tier. Skip enqueue if
            # less than interval_s has elapsed since this PID was last written.
            # This implements the 1s/5s/30s tier architecture without needing
            # multiple async connections.
            now_mono = time.monotonic()
            if now_mono - self._last_enqueue[pid.command.name] < pid.interval_s:
                return
            self._last_enqueue[pid.command.name] = now_mono

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
