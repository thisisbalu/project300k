from logger import logger


class Collector:
    # TODO: Task 9 — register async watchers, implement callbacks

    def __init__(self, obd_connection, queue_writer, trip_manager):
        self._obd = obd_connection
        self._queue_writer = queue_writer
        self._trip_manager = trip_manager

    def start(self) -> None:
        # TODO: Task 9 — register all PID watchers (standard + Ford Mode 22)
        pass

    def stop(self) -> None:
        # TODO: Task 9 — stop all watchers
        pass

    def _make_callback(self, table: str, column: str):
        # TODO: Task 9 — return callback that enqueues row with UUID + trip_id + timestamp
        pass
