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

The 30s threshold prevents a long red light from ending the trip.
The voltage check prevents accessory mode (battery ~12V, RPM=0)
from being mistaken for engine running.

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

from logger import logger


class TripManager:
    """Detects trip boundaries and manages the current trip_id.

    Receives RPM and voltage updates from collector callbacks and
    maintains the state machine for trip start/end detection.

    Attributes:
        current_trip_id: UUID of the active trip, or None between trips.
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
        self._rpm_zero_since: float | None = None  # monotonic time when RPM last hit 0
        self._last_voltage: float | None = None

    def on_rpm(self, value) -> None:
        """Handle an incoming RPM reading.

        Checks trip start/end conditions and manages the 30s RPM=0 timer
        for polling pause and trip end detection.

        Args:
            value: OBDResponse from python-obd (may be None on read error).
        """
        # TODO: Task 10 — implement trip start/end detection and polling pause
        pass

    def on_voltage(self, value) -> None:
        """Handle an incoming battery voltage reading.

        Stores the latest voltage for use in trip boundary checks.

        Args:
            value: OBDResponse from python-obd (may be None on read error).
        """
        # TODO: Task 10 — store voltage, confirm trip start/end conditions
        pass

    def _start_trip(self) -> None:
        """Begin a new trip — generate UUID, persist row, scan DTCs.

        Called when both voltage > 13.0V and RPM > 0 are observed
        for the first time after the previous trip ended.
        """
        # TODO: Task 10 — generate UUID, write to trips table, trigger DTC scan
        pass

    def _end_trip(self) -> None:
        """Close the current trip — update end_time, scan DTCs.

        Called when RPM=0 for >30s and voltage < 12.5V are both observed.
        """
        # TODO: Task 10 — update trips.end_time, trigger DTC scan
        pass

    def _scan_dtc(self) -> None:
        """Run a full DTC scan and persist any codes to dtc_events.

        Called at trip start and trip end. Uses Mode 03 (GET_DTC) which
        returns all stored fault codes from the PCM.
        """
        # TODO: Task 11 — run GET_DTC, write each code to dtc_events table
        pass
