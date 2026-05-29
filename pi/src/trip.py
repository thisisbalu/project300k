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

Polling pause (separate from trip end):
    When RPM=0 for >30s, the 1s and 5s polling tiers are paused to avoid
    collecting meaningless zero-RPM rows. The 30s tier keeps running —
    battery voltage at rest is useful health data. Polling resumes
    immediately when RPM > 0.

On Bluetooth drop mid-trip:
    The same trip_id is kept after reconnection. The gap in obd_1s data
    is honest and acceptable — splitting a single drive into multiple
    trips on a BT glitch fragments trip-level analysis.
"""

import time
import uuid
from datetime import datetime, timezone

import obd

from logger import logger

# Trip boundary thresholds
VOLTAGE_ENGINE_RUNNING  = 13.0   # V — alternator running above this
VOLTAGE_ENGINE_OFF      = 12.5   # V — alternator stopped below this
RPM_ZERO_DURATION_S     = 30     # seconds RPM must be 0 before trip ends / polling pauses


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
    """

    def __init__(self, queue_writer, obd_connection) -> None:
        """Initialise with dependencies needed for writes and DTC scans.

        Args:
            queue_writer:   QueueWriter for persisting trip rows.
            obd_connection: OBDConnection for issuing DTC scan commands.
        """
        self._queue_writer = queue_writer
        self._obd_connection = obd_connection
        self.current_trip_id: str | None = None

        # Monotonic clock used for the RPM=0 duration timer.
        # Monotonic avoids false triggers if the system clock jumps (NTP sync).
        self._rpm_zero_since: float | None = None

        self._last_voltage: float | None = None
        self._polling_paused: bool = False

    def on_rpm(self, response: obd.OBDResponse) -> None:
        """Handle an incoming RPM reading.

        Drives the trip start/end state machine and the polling pause logic.

        Args:
            response: OBDResponse from python-obd (may be null on read error).
        """
        if response is None or response.is_null():
            return

        rpm = response.value.magnitude

        if rpm > 0:
            self._rpm_zero_since = None

            # Resume paused polling tiers when engine starts again.
            if self._polling_paused:
                self._resume_polling()

            # Start a trip if voltage confirms the engine is running.
            if self.current_trip_id is None and self._voltage_above(VOLTAGE_ENGINE_RUNNING):
                self._start_trip()

        else:
            # rpm == 0 — start or continue the zero-duration timer.
            if self._rpm_zero_since is None:
                self._rpm_zero_since = time.monotonic()

            elapsed = time.monotonic() - self._rpm_zero_since

            if elapsed >= RPM_ZERO_DURATION_S:
                # Pause polling after 30s of RPM=0 to avoid noisy zero rows.
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
        self._last_voltage = response.value.magnitude

    def _start_trip(self) -> None:
        """Begin a new trip — generate UUID, persist trips row, scan DTCs."""
        self.current_trip_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        row = {
            "id": self.current_trip_id,
            "start_time": now,
            "end_time": None,
            "distance_km": None,  # calculated post-sync in PostgreSQL
            "duration_s": None,   # calculated at trip end
            "synced": 0,
        }
        self._queue_writer.enqueue("trips", row)
        logger.info(f"Trip started: {self.current_trip_id}")
        self._scan_dtc()

    def _end_trip(self) -> None:
        """Close the current trip — update end_time, calculate duration, scan DTCs."""
        now = datetime.now(timezone.utc).isoformat()

        # Update the existing trips row with end_time.
        # QueueWriter INSERT will conflict on id — storage.py uses
        # INSERT OR REPLACE so the end_time is written correctly.
        # TODO: revisit when schema is finalised — may need a dedicated UPDATE path.
        row = {
            "id": self.current_trip_id,
            "end_time": now,
            "synced": 0,
        }
        self._queue_writer.enqueue("trips_update", row)
        logger.info(f"Trip ended: {self.current_trip_id}")
        self._scan_dtc()
        self.current_trip_id = None

    def _scan_dtc(self) -> None:
        """Run a full DTC scan and persist any codes to dtc_events.

        Called at trip start and trip end. Uses Mode 03 (GET_DTC) which
        returns all stored fault codes from the PCM.

        Each DTC is written as a separate row in dtc_events. If the scan
        returns no codes, logs a clean confirmation. If the OBD connection
        is not available, logs a warning and skips — DTC scan is best-effort.
        """
        if not self._obd_connection.is_connected:
            logger.warning("DTC scan skipped — OBD not connected")
            return

        try:
            response = self._obd_connection.connection.query(obd.commands.GET_DTC)

            if response.is_null() or not response.value:
                logger.info("DTC scan clean — no fault codes")
                return

            now = datetime.now(timezone.utc).isoformat()

            for code, description in response.value:
                row = {
                    "id": str(uuid.uuid4()),
                    "trip_id": self.current_trip_id,
                    "timestamp": now,
                    "code": code,
                    "description": description,
                    # DTCs from GET_DTC are stored codes — pending codes require
                    # a separate Mode 07 query which is not implemented yet.
                    "status": "stored",
                    "synced": 0,
                }
                self._queue_writer.enqueue("dtc_events", row)
                logger.warning(f"DTC detected: {code} — {description}")

            logger.info(f"DTC scan complete — {len(response.value)} code(s) found")

        except Exception as e:
            logger.error(f"DTC scan failed: {e}")

    def _pause_polling(self) -> None:
        """Pause 1s and 5s polling tiers — engine is off or idling long."""
        self._polling_paused = True
        logger.info("Polling paused — RPM=0 for >30s (30s tier still active)")

    def _resume_polling(self) -> None:
        """Resume 1s and 5s polling tiers — engine started again."""
        self._polling_paused = False
        logger.info("Polling resumed — RPM > 0")

    def _voltage_above(self, threshold: float) -> bool:
        """Return True if the last known voltage is above threshold."""
        return self._last_voltage is not None and self._last_voltage > threshold

    def _voltage_below(self, threshold: float) -> bool:
        """Return True if the last known voltage is below threshold."""
        return self._last_voltage is not None and self._last_voltage < threshold
