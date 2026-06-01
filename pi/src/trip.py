"""
trip.py — Trip lifecycle detection and polling pause management.

A trip is a continuous engine-on period, identified by a UUID. Trip
boundaries are determined by two signals — RPM and battery voltage —
to avoid false starts/ends from accessories or brief engine stalls.

Trip start conditions (both required):
    battery_v > 13.0V  — alternator running, engine is on
    rpm > 0            — engine actually started, not just accessory mode

Trip end conditions (both required):
    rpm = 0 for > 30s  — engine off (not just a red light)
    battery_v < 12.5V  — alternator stopped, confirms engine off

The 30s threshold prevents a long red light or drive-through from ending
the trip. The voltage check prevents accessory mode (battery ~12V, RPM=0)
from being mistaken for engine off — voltage stays at ~13.8V while the
engine is running even if RPM briefly reads 0.

Threading:
    on_rpm() and on_voltage() are called from python-obd's background
    polling thread. All shared state (current_trip_id, _rpm_zero_since,
    _last_voltage, _polling_paused) is protected by _lock to prevent
    data races between concurrent callback invocations.

    DTC scans run on daemon threads. stop() joins them with a timeout so
    obd_connection.disconnect() is not called while a scan is mid-query.

On Bluetooth drop mid-trip:
    The same trip_id is kept after reconnection. The gap in obd_1s data
    is honest and acceptable — splitting a single drive into multiple
    trips on a BT glitch fragments trip-level analysis.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone

import obd

from health import _read_collector_version
from logger import logger
from storage import get_trip_number, update_trip_end

# Trip boundary thresholds
VOLTAGE_ENGINE_RUNNING = 13.0  # V — alternator running above this
VOLTAGE_ENGINE_OFF     = 12.5  # V — alternator stopped below this
RPM_ZERO_DURATION_S    = 30    # seconds RPM must be 0 before trip ends / polling pauses


class TripManager:
    """Detects trip boundaries and manages the current trip_id.

    Receives RPM and voltage updates from Collector callbacks and
    maintains a simple state machine for trip start/end detection.

    State:
        No active trip  (current_trip_id is None)
        Active trip     (current_trip_id is set)

    Transitions:
        No trip  → Active:  voltage > 13.0V AND rpm > 0
        Active   → No trip: rpm = 0 for > 30s AND voltage < 12.5V

    Attributes:
        current_trip_id: UUID string of the active trip, or None between trips.
                         Read by Collector callbacks — protected by _lock.
        is_paused:       True when 1s/5s polling is suppressed (RPM=0 for >30s).
                         Read by Collector._make_callback() — no lock needed
                         because it is a single bool written atomically.
    """

    def __init__(self, queue_writer, obd_connection=None) -> None:
        """Initialise with dependencies needed for writes and DTC scans.

        Args:
            queue_writer:   QueueWriter for persisting trip rows and
                            issuing the trip-end UPDATE via direct_execute().
            obd_connection: Unused — kept for backward compatibility only.
                            DTC queries now go through _dtc_query_fn wired
                            via set_dtc_query() after Collector is created.
        """
        self._queue_writer = queue_writer
        # _dtc_query_fn is wired after Collector is created in main.py via
        # set_dtc_query(). None until wired — DTC scans are skipped in that
        # window (only matters at first trip start, which cannot fire until
        # collector.start() has run and wired the callable).
        self._dtc_query_fn = None

        # Lock protecting all shared state accessed from callback threads.
        # on_rpm and on_voltage fire from python-obd's background thread;
        # current_trip_id is read by Collector callbacks on the same thread.
        # Without this lock, two rapid RPM=0→RPM>0 callbacks could both see
        # current_trip_id=None and both call _start_trip(), creating duplicate trips.
        self._lock = threading.Lock()

        self.current_trip_id: str | None = None

        # Monotonic clock for the RPM=0 duration timer.
        # Monotonic avoids false triggers if the system clock jumps (NTP sync).
        self._rpm_zero_since: float | None = None

        self._last_voltage: float | None = None
        self._polling_paused: bool = False

        # Track active DTC scan threads so stop() can join them before
        # obd_connection.disconnect() closes the connection they are using.
        self._dtc_threads: list[threading.Thread] = []
        self._dtc_threads_lock = threading.Lock()

    @property
    def is_paused(self) -> bool:
        """True when 1s/5s polling is suppressed (RPM=0 for >30s).

        Read by Collector._make_callback() on the OBD callback thread.
        Written by _pause_polling()/_resume_polling() under _lock.
        A single bool read/write is atomic in CPython, so no separate
        lock is required for this read-only property.
        """
        return self._polling_paused

    def on_rpm(self, response: obd.OBDResponse) -> None:
        """Handle an incoming RPM reading.

        Drives the trip start/end state machine and the polling pause logic.
        All shared state mutations are protected by _lock.

        Args:
            response: OBDResponse from python-obd (may be null on read error).
        """
        if response is None or response.is_null():
            return

        rpm = response.value.magnitude

        with self._lock:
            if rpm > 0:
                self._rpm_zero_since = None

                if self._polling_paused:
                    self._resume_polling()

                # Start trip only if no trip is currently active AND voltage
                # confirms the alternator is running. Both checks happen inside
                # the lock so concurrent callbacks cannot both enter _start_trip().
                if self.current_trip_id is None and self._voltage_above(VOLTAGE_ENGINE_RUNNING):
                    self._start_trip()

            else:
                # rpm == 0 — start or continue the zero-duration timer.
                if self._rpm_zero_since is None:
                    self._rpm_zero_since = time.monotonic()

                elapsed = time.monotonic() - self._rpm_zero_since

                if elapsed >= RPM_ZERO_DURATION_S:
                    if not self._polling_paused:
                        self._pause_polling()

                    # End the trip only if voltage also confirms engine is off.
                    if self.current_trip_id is not None and self._voltage_below(VOLTAGE_ENGINE_OFF):
                        self._end_trip()

    def on_voltage(self, response: obd.OBDResponse) -> None:
        """Handle an incoming battery voltage reading.

        Stores the latest voltage for use in trip boundary checks performed
        by on_rpm(). Voltage alone does not trigger trip start/end — RPM
        is the primary signal.

        Args:
            response: OBDResponse from python-obd (may be null on read error).
        """
        if response is None or response.is_null():
            return
        with self._lock:
            self._last_voltage = response.value.magnitude

    def set_dtc_query(self, fn) -> None:
        """Wire the callable used to issue DTC queries at trip boundaries.

        Must be called after Collector is created (in main.py) because
        Collector owns the single OBD connection. fn is Collector.query_sync,
        which stops the async loop, queries GET_DTC, and restarts the loop —
        preventing byte-race contention with the async polling thread.

        Args:
            fn: Callable(obd.OBDCommand) → OBDResponse | None.
                Returns None when the async connection is not available.
        """
        self._dtc_query_fn = fn

    def stop(self) -> None:
        """Wait for any in-flight DTC scan threads to complete.

        Called from main.py's finally block after collector.stop() and before
        obd_connection.disconnect(), so the scan threads finish their query()
        calls before the underlying serial connection is closed.
        """
        with self._dtc_threads_lock:
            threads = list(self._dtc_threads)
        for t in threads:
            t.join(timeout=5)
        logger.info("TripManager stopped")

    def _start_trip(self) -> None:
        """Begin a new trip — generate UUID, persist trips row, scan DTCs.

        Must be called with _lock held. _scan_dtc() is dispatched after
        state is set so its serial I/O does not block the lock.
        """
        self.current_trip_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        # get_trip_number reads committed trips. If the previous trip's INSERT
        # is still pending in the QueueWriter batch, the count may be one low,
        # producing a non-sequential trip_number. This is cosmetic only — the
        # UUID is the authoritative trip identifier.
        trip_number = get_trip_number(self._queue_writer.conn)
        row = {
            "id": self.current_trip_id,
            "trip_number": trip_number,
            "start_time": now,
            "end_time": None,
            "distance_km": None,          # calculated post-sync in PostgreSQL
            "duration_s": None,           # written at trip end
            "collector_version": _read_collector_version(),
            "synced": 0,
        }
        self._queue_writer.enqueue("trips", row)
        logger.info(f"Trip started: {self.current_trip_id}")

        # Capture trip_id before releasing the lock — DTC scan runs outside
        # the lock so its duration does not block on_rpm/on_voltage callbacks.
        trip_id = self.current_trip_id
        self._dispatch_dtc_scan(trip_id, "trip_start")

    def _end_trip(self) -> None:
        """Close the current trip — write end_time, calculate duration, scan DTCs.

        Must be called with _lock held. Uses update_trip_end() which routes
        through QueueWriter.direct_execute() so the UPDATE is serialised
        against the writer thread's INSERT batches via _db_lock.
        """
        end_time = datetime.now(timezone.utc).isoformat()
        trip_id = self.current_trip_id

        # update_trip_end() calls queue_writer.direct_execute() which acquires
        # _db_lock — this prevents the UPDATE from racing the writer thread's
        # concurrent INSERT batch on the same SQLite connection.
        update_trip_end(self._queue_writer, trip_id, end_time)

        logger.info(f"Trip ended: {trip_id}")
        self.current_trip_id = None

        # DTC scan dispatched to a background thread — see _scan_dtc() note.
        self._dispatch_dtc_scan(trip_id, "trip_end")

    def _dispatch_dtc_scan(self, trip_id: str, scan_trigger: str) -> None:
        """Dispatch a DTC scan to a background thread.

        Calling query() synchronously from an on_rpm/on_voltage callback
        (which fires on python-obd's polling thread) contends with the async
        loop for the serial port, risking deadlock. Running the scan on a
        separate daemon thread avoids this — the scan completes independently
        without blocking the polling loop.

        The thread is tracked in _dtc_threads so stop() can join it before
        obd_connection.disconnect() is called.

        Args:
            trip_id:      UUID of the trip this scan belongs to.
            scan_trigger: "trip_start" or "trip_end".
        """
        t = threading.Thread(
            target=self._scan_dtc,
            args=(trip_id, scan_trigger),
            daemon=True,
            name=f"dtc-scan-{scan_trigger}",
        )
        with self._dtc_threads_lock:
            self._dtc_threads.append(t)
        t.start()

    def _scan_dtc(self, trip_id: str, scan_trigger: str) -> None:
        """Scan for stored (Mode 03) and pending (Mode 07) DTCs.

        Called at trip start and trip end via _dispatch_dtc_scan(). Both scan
        modes run independently — a clean Mode 03 does not skip Mode 07.

        Mode 03 (GET_DTC): stored codes, MIL triggered, confirmed faults.
        Mode 07 (GET_CURRENT_DTC): pending codes, occurred this drive cycle
            but not yet MIL-triggered. These are the early-warning signals
            the longevity system is built around — a code that appears pending
            for two consecutive trips becomes stored on the third.

        Each DTC is stored as a separate dtc_events row with status "stored"
        or "pending". Both modes share the same timestamp so rows from the
        same scan can be correlated by (trip_id, timestamp, scan_trigger).

        Args:
            trip_id:      UUID of the trip this scan belongs to.
            scan_trigger: "trip_start" or "trip_end" — recorded in dtc_events.
        """
        if self._dtc_query_fn is None:
            logger.warning("DTC scan skipped — OBD query not wired (set_dtc_query not called)")
            return

        try:
            stored_response = self._dtc_query_fn(obd.commands.GET_DTC)
            if stored_response is None:
                logger.warning("DTC scan skipped — OBD not connected")
                return

            now = datetime.now(timezone.utc).isoformat()
            stored_count  = 0
            pending_count = 0

            if not stored_response.is_null() and stored_response.value:
                for code, description in stored_response.value:
                    self._queue_writer.enqueue("dtc_events", {
                        "id":           str(uuid.uuid4()),
                        "trip_id":      trip_id,
                        "timestamp":    now,
                        "code":         code,
                        "description":  description,
                        "status":       "stored",
                        "scan_trigger": scan_trigger,
                        "synced":       0,
                    })
                    logger.warning(f"DTC detected: {code} — {description}")
                stored_count = len(stored_response.value)

            # Mode 07 — pending codes (two consecutive occurrences become stored)
            pending_response = self._dtc_query_fn(obd.commands.GET_CURRENT_DTC)
            if pending_response is not None and not pending_response.is_null() and pending_response.value:
                for code, description in pending_response.value:
                    self._queue_writer.enqueue("dtc_events", {
                        "id":           str(uuid.uuid4()),
                        "trip_id":      trip_id,
                        "timestamp":    now,
                        "code":         code,
                        "description":  description,
                        "status":       "pending",
                        "scan_trigger": scan_trigger,
                        "synced":       0,
                    })
                    logger.warning(f"Pending DTC: {code} — {description}")
                pending_count = len(pending_response.value)

            if stored_count == 0 and pending_count == 0:
                logger.info(f"DTC scan clean ({scan_trigger}) — no stored or pending fault codes")
            else:
                logger.info(
                    f"DTC scan complete ({scan_trigger}) — "
                    f"{stored_count} stored, {pending_count} pending"
                )

        except Exception as e:
            logger.error(f"DTC scan failed ({scan_trigger}): {e}")

    def _pause_polling(self) -> None:
        """Pause 1s and 5s polling tiers — RPM=0 for >30s."""
        self._polling_paused = True
        logger.info("Polling paused — RPM=0 for >30s (30s tier still active)")

    def _resume_polling(self) -> None:
        """Resume 1s and 5s polling tiers — RPM > 0."""
        self._polling_paused = False
        logger.info("Polling resumed — RPM > 0")

    def _voltage_above(self, threshold: float) -> bool:
        """Return True if the last known voltage exceeds threshold."""
        return self._last_voltage is not None and self._last_voltage > threshold

    def _voltage_below(self, threshold: float) -> bool:
        """Return True if the last known voltage is below threshold."""
        return self._last_voltage is not None and self._last_voltage < threshold
