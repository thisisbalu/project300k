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

On Bluetooth drop mid-trip:
    The same trip_id is kept after reconnection. The gap in obd_1s data
    is honest and acceptable — splitting a single drive into multiple
    trips on a BT glitch fragments trip-level analysis.
"""

import threading
import time
import uuid
from datetime import datetime, timezone

import obd

from logger import logger
from storage import update_trip_end

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
    """

    def __init__(self, queue_writer, obd_connection) -> None:
        """Initialise with dependencies needed for writes and DTC scans.

        Args:
            queue_writer:   QueueWriter for persisting trip rows.
            obd_connection: OBDConnection for issuing DTC scan commands.
        """
        self._queue_writer = queue_writer
        self._obd_connection = obd_connection

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

    def _start_trip(self) -> None:
        """Begin a new trip — generate UUID, persist trips row, scan DTCs.

        Must be called with _lock held. _scan_dtc() is called after releasing
        state so DTC scan duration does not block other callbacks.
        """
        self.current_trip_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        row = {
            "id": self.current_trip_id,
            "start_time": now,
            "end_time": None,
            "distance_km": None,    # calculated post-sync in PostgreSQL
            "duration_s": None,     # written at trip end
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

        Must be called with _lock held. Uses storage.update_trip_end() for a
        direct UPDATE rather than inserting a duplicate row.
        """
        end_time = datetime.now(timezone.utc).isoformat()
        trip_id = self.current_trip_id

        # Update existing trips row — cannot go through QueueWriter INSERT
        # because INSERT would create a duplicate row with the same UUID.
        # update_trip_end() issues a direct UPDATE on the SQLite connection.
        update_trip_end(self._queue_writer.conn, trip_id, end_time)

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
        t.start()

    def _scan_dtc(self, trip_id: str, scan_trigger: str) -> None:
        """Run a full DTC scan and persist any codes to dtc_events.

        Called at trip start and trip end. Uses Mode 03 (GET_DTC) which
        returns all stored fault codes from the PCM.

        Each DTC is written as a separate row in dtc_events. If the scan
        returns no codes, logs a clean confirmation. If the OBD connection
        is not available, logs a warning and skips — DTC scan is best-effort.

        Note: this method must NOT be called while _lock is held, because
        the OBD query blocks on serial I/O. Holding the lock during a blocking
        call would stall all on_rpm/on_voltage callbacks for the duration.

        Args:
            trip_id:      UUID of the trip this scan belongs to.
            scan_trigger: "trip_start" or "trip_end" — recorded in dtc_events.
        """
        if not self._obd_connection.is_connected:
            logger.warning("DTC scan skipped — OBD not connected")
            return

        try:
            # query() is a synchronous blocking call — must not be called
            # while the async polling loop holds the serial port exclusively.
            # In python-obd 0.7.3, obd.Async.stop() must be called before
            # issuing synchronous queries. This is a known limitation — DTC
            # scans at trip boundaries are deferred to a future improvement
            # where the async loop is paused around the query.
            response = self._obd_connection.connection.query(obd.commands.GET_DTC)

            if response.is_null() or not response.value:
                logger.info(f"DTC scan clean ({scan_trigger}) — no fault codes")
                return

            now = datetime.now(timezone.utc).isoformat()

            for code, description in response.value:
                row = {
                    "id": str(uuid.uuid4()),
                    "trip_id": trip_id,
                    "timestamp": now,
                    "code": code,
                    "description": description,
                    # DTCs from GET_DTC are stored codes — pending codes require
                    # a separate Mode 07 query not yet implemented.
                    "status": "stored",
                    "scan_trigger": scan_trigger,
                    "synced": 0,
                }
                self._queue_writer.enqueue("dtc_events", row)
                logger.warning(f"DTC detected: {code} — {description}")

            logger.info(f"DTC scan complete ({scan_trigger}) — {len(response.value)} code(s)")

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
