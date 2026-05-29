from logger import logger


class TripManager:
    # TODO: Task 10 — implement trip start/end detection, polling pause logic

    def __init__(self, queue_writer, obd_connection):
        self._queue_writer = queue_writer
        self._obd_connection = obd_connection
        self.current_trip_id = None
        self._rpm_zero_since = None

    def on_rpm(self, value) -> None:
        # TODO: Task 10 — handle RPM updates, detect trip start/end, pause polling
        pass

    def on_voltage(self, value) -> None:
        # TODO: Task 10 — handle voltage updates, confirm trip start/end
        pass

    def _start_trip(self) -> None:
        # TODO: Task 10 — generate UUID, write trips row, trigger DTC scan
        pass

    def _end_trip(self) -> None:
        # TODO: Task 10 — update trips.end_time, trigger DTC scan
        pass

    def _scan_dtc(self) -> None:
        # TODO: Task 11 — run GET_DTC, write to dtc_events
        pass
