"""
collector.py — Async OBD PID watcher registration and callback handling.

Sets up python-obd's async polling loop with one watcher per PID. Each
watcher fires its callback at the configured interval. The callback builds
a row dict and hands it to QueueWriter — it never writes to SQLite directly,
ensuring all database access is serialised through the queue.

All PIDs are registered from ALL_PIDS in obd_commands.py, so adding or
removing a PID requires no changes here — only in obd_commands.py.

Row shape — one combined row per table per interval tick:
    All PIDs in the same table share a per-table buffer. Each callback
    accumulates its column value into the buffer. When the table's interval_s
    elapses, the buffer is flushed as a single combined row — obd_1s gets one
    row/second with rpm, speed_kmh, throttle_pct, load_pct all populated, not
    four separate sparse rows. This matches the schema design intent.

Each callback:
    1. Receives an OBDResponse from python-obd
    2. Checks trip_id — skips if no active trip (avoids NOT NULL violation)
    3. Extracts the magnitude value (None if response is null/error)
    4. If the table's interval_s has elapsed: flush the buffer as one combined
       row via queue_writer.enqueue(), then reset the buffer and timer
    6. Accumulates this PID's value into the buffer for the current window

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
from obd_connection import connect_with_timeout


class Collector:
    """Registers and manages all OBD async PID watchers.

    Opens its own obd.Async connection using the configured OBD port.
    Delegates trip context (trip_id) to TripManager and row persistence
    to QueueWriter. Monitors the connection health and reconnects on drop.

    Attributes:
        _async_conn:        python-obd Async connection managing the polling loop.
        _table_buffer:      Per-table dict of accumulated column values for the
                            current interval window. Flushed as one combined row
                            when the table's interval_s elapses.
        _table_last_flush:  Monotonic timestamp of the last flush per table.
                            Reset on each (re)connect so the first window starts
                            cleanly.
        _stop_event:        Signals the monitor thread to stop.
        _monitor_thread:    Background thread checking connection health every 10s.
    """

    def __init__(self, queue_writer, trip_manager, obd_connection) -> None:
        """Initialise with required dependencies.

        Args:
            queue_writer:   QueueWriter for thread-safe SQLite writes.
            trip_manager:   TripManager providing current trip_id and
                            receiving RPM/voltage updates for trip detection.
            obd_connection: OBDConnection for reconnect() and reconnect_count.
        """
        self._queue_writer = queue_writer
        self._trip_manager = trip_manager
        self._obd_connection = obd_connection
        self._async_conn: obd.Async | None = None
        self._table_buffer: dict[str, dict] = {}
        self._table_last_flush: dict[str, float] = {}
        # Tracks the monotonic timestamp of the last non-NULL response per column.
        # Written from the OBD async thread, read from the main thread for heartbeat.
        # Single-key dict writes are atomic under the GIL — no lock needed.
        self._pid_last_seen: dict[str, float] = {}
        # Tracks the most recent non-NULL value per column for heartbeat display.
        self._latest: dict[str, object] = {}
        # Serialises concurrent query_sync() calls — prevents two DTC scans
        # (one at trip_start and one at trip_end if they overlap) from racing
        # to stop/restart the same async connection.
        self._dtc_lock = threading.Lock()
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
        tables = {pid.table for pid in ALL_PIDS}
        logger.info(f"Collector started — {len(ALL_PIDS)} PIDs active across {len(tables)} tables")

    def stop(self) -> None:
        """Stop the monitor thread, all watchers, and close the async polling loop."""
        self._stop_event.set()
        if self._async_conn is not None:
            self._async_conn.stop()
            self._async_conn = None
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=5)
        logger.info("Collector stopped")

    @property
    def is_async_connected(self) -> bool:
        """True if the async OBD connection is currently active."""
        return self._async_conn is not None and self._async_conn.is_connected()

    def is_monitor_alive(self) -> bool:
        """True if the reconnect-monitor thread is running.

        Read by the main watchdog loop. If the monitor thread has died the
        collector can no longer recover from a Bluetooth drop, so the service
        must be restarted rather than left pinging the watchdog as a zombie.
        """
        return self._monitor_thread is not None and self._monitor_thread.is_alive()

    def query_sync(self, command: obd.OBDCommand) -> obd.OBDResponse | None:
        """Stop the async loop, issue a synchronous OBD query, restart the loop.

        Used by TripManager._scan_dtc() for DTC scans at trip boundaries.
        Stopping the async loop before querying prevents both from competing
        for bytes on the same /dev/rfcomm0 serial device simultaneously —
        without this, the async polling thread consumes the DTC response bytes
        and the query() call times out.

        The polling gap while the scan runs is brief (< 5s for GET_DTC) and
        only occurs at trip start/end, so the impact on continuous data
        collection is minimal.

        Returns:
            OBDResponse on success, None if not connected or on error.
        """
        with self._dtc_lock:
            # Snapshot the connection once. _monitor_connection() may null
            # self._async_conn from another thread during a reconnect; binding
            # it to a local here means a mid-scan reconnect cannot turn the
            # calls below into a None dereference.
            conn = self._async_conn
            if conn is None or not conn.is_connected():
                return None
            try:
                conn.stop()
                response = conn.query(command)
                conn.start()
                return response
            except Exception as e:
                logger.error(f"DTC query error: {e}")
                # Best-effort restart so data collection continues.
                try:
                    conn.start()
                except Exception:
                    pass
                return None

    def _connect_and_watch(self) -> None:
        """Open obd.Async and register all PID watchers.

        Extracted so it can be called both at start() and during reconnect
        inside _monitor_connection() without duplicating registration logic.
        """
        # obd.Async is a subclass of obd.OBD — instantiate with port string,
        # not by wrapping an existing connection object. Wrapped in
        # connect_with_timeout because obd.Async.__init__ shares obd.OBD's
        # init path and can hang indefinitely on a stale rfcomm0 — without the
        # guard a mid-trip reconnect blocks this thread forever while the main
        # loop keeps pinging the watchdog, so systemd never restarts the service.
        self._async_conn = connect_with_timeout(
            lambda: obd.Async(config.OBD_PORT, fast=False, timeout=30)
        )

        # Reset per-table buffers and flush timers on every (re)connect.
        # Setting last_flush to 0.0 means the first callback for each table
        # sees elapsed >> interval_s, resets the timer, and starts a clean
        # window — no stale data from a previous connection is carried forward.
        for pid in ALL_PIDS:
            self._table_buffer[pid.table] = {}
            self._table_last_flush[pid.table] = 0.0
        self._pid_last_seen.clear()
        self._latest.clear()

        for pid in ALL_PIDS:
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
            # Snapshot the reference — stop() may null it from another thread.
            conn = self._async_conn
            if conn is not None and conn.is_connected():
                continue

            # Down — either a real drop or a previous reconnect that failed and
            # left _async_conn None. Keep retrying every 10s; a single failure
            # must NOT permanently disable reconnection (would silently end
            # collection for the rest of the session while the watchdog keeps
            # getting pinged).
            if self._stop_event.is_set():
                break
            logger.warning("Async OBD connection down — reconnecting")
            try:
                if conn is not None:
                    conn.stop()
                self._async_conn = None
                # reconnect() increments reconnect_count and writes it to disk
                # so the sync script can include the live count.
                self._obd_connection.reconnect()
                self._connect_and_watch()
                logger.info("Async OBD connection restored")
            except Exception as e:
                logger.error(f"Async OBD reconnect failed: {e} — will retry in 10s")

    def polling_health(self, window_s: int = 300) -> tuple[int, int]:
        """Return (active_pids, total_pids) for the given look-back window.

        A PID is considered active if it returned a non-NULL value within
        the last window_s seconds. Used by the heartbeat log to surface
        consistently-failing PIDs without querying SQLite.

        Args:
            window_s: Look-back window in seconds. Default 300s (5 minutes).

        Returns:
            Tuple of (pids_seen_in_window, total_pids_registered).
        """
        cutoff = time.monotonic() - window_s
        active = sum(1 for t in self._pid_last_seen.values() if t >= cutoff)
        return active, len(ALL_PIDS)

    def latest(self, column: str) -> object:
        """Return the most recent non-NULL value for a column, or None.

        Used by the heartbeat log to show current rpm and speed_kmh without
        querying SQLite or coupling main.py to the internal buffer structure.

        Args:
            column: Column name (e.g. 'rpm', 'speed_kmh').

        Returns:
            Most recent non-NULL value, or None if never seen.
        """
        return self._latest.get(column)

    def _make_callback(self, pid: PIDConfig):
        """Return a closure that buffers OBD readings and flushes combined rows.

        The closure captures the PIDConfig. All PIDs sharing the same table
        accumulate their column values into a shared per-table buffer. When
        the table's interval_s elapses the buffer is flushed as one combined
        row — so obd_1s gets a single row/second with all four columns
        populated rather than four separate single-column sparse rows.

        python-obd 0.7.3 fires all callbacks at the async loop rate (~1Hz).
        The interval is enforced at the table level: the buffer is only flushed
        once per interval_s, regardless of how many times callbacks fire.

        Flush timing:
            The first callback for a table after (re)connect resets the window
            timer and discards any empty buffer — no row is emitted until the
            first full window completes. This gives all PIDs in the tier a
            chance to contribute a value before the first flush.

        NULL handling:
            None is accumulated for bad/missing responses and appears as NULL
            in the flushed row. A NULL spike for a specific column is a
            diagnostic signal (sensor fault, BT glitch, wrong Mode 22 address).

        Args:
            pid: PIDConfig carrying table, column, and interval for this PID.

        Returns:
            Callable that accepts an OBDResponse and accumulates/flushes.
        """
        def callback(response: obd.OBDResponse) -> None:
            # Never let an exception escape into python-obd's async worker thread:
            # an unhandled error there kills the polling thread permanently while
            # is_connected() stays True (so the monitor never reconnects) and the
            # watchdog keeps being pinged — silently halting all data collection.
            try:
                self._handle_response(pid, response)
            except Exception as e:
                logger.error(f"OBD callback error for {pid.table}.{pid.column}: {e}")

        return callback

    def _handle_response(self, pid: PIDConfig, response: obd.OBDResponse) -> None:
        """Buffer one OBD reading and flush the table's combined row when due.

        Runs on python-obd's async callback thread. Kept separate from the
        callback closure so the closure can wrap it in a try/except that stops
        any exception from escaping into (and killing) the async worker thread.
        """
        table      = pid.table
        column     = pid.column
        interval_s = pid.interval_s

        trip_id = self._trip_manager.current_trip_id
        if trip_id is None:
            return

        # Extract raw magnitude — None if response has no value.
        value = None
        if response is not None and not response.is_null():
            value = (
                response.value.magnitude
                if hasattr(response.value, "magnitude")
                else response.value
            )

        now_mono = time.monotonic()

        if value is not None:
            self._pid_last_seen[column] = now_mono
            self._latest[column] = value
        buf      = self._table_buffer[table]

        # Flush the completed window when interval_s has elapsed.
        # Always reset the timer (even on an empty first window) so
        # subsequent callbacks see a valid elapsed baseline.
        if now_mono - self._table_last_flush[table] >= interval_s:
            if buf:
                row = {
                    "id":        str(uuid.uuid4()),
                    "trip_id":   trip_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "synced":    0,
                    **buf,
                }
                self._queue_writer.enqueue(table, row)
            self._table_buffer[table]     = {}
            self._table_last_flush[table] = now_mono

        # Accumulate into the current window — overwrites if the same column
        # fires multiple times before the window closes (takes the most recent
        # value, which is correct for time-series data).
        self._table_buffer[table][column] = value
