from logger import logger
from config import config


class OBDConnection:
    # TODO: Task 8 — implement connection, retry logic, reconnect tracking

    def __init__(self):
        self._connection = None
        self.reconnect_count = 0

    def connect(self):
        # TODO: Task 8 — connect with fast=False, timeout=30, retry every 15s
        pass

    def reconnect(self):
        # TODO: Task 8 — reconnect mid-trip, increment reconnect_count
        pass

    def disconnect(self):
        # TODO: Task 8 — clean disconnect
        pass

    @property
    def is_connected(self) -> bool:
        return self._connection is not None and self._connection.is_connected()
