import queue
import threading
from typing import Any
from logger import logger


class QueueWriter:
    # TODO: Task 6 — implement drain loop and SQLite writes
    def __init__(self, conn):
        self._queue: queue.Queue = queue.Queue()
        self._conn = conn
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._drain, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join()

    def enqueue(self, table: str, row: dict[str, Any]) -> None:
        self._queue.put((table, row))

    def _drain(self) -> None:
        # TODO: Task 6 — drain queue and write rows to SQLite
        pass
