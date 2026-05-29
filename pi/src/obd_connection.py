"""
obd_connection.py — OBD connection management with retry and reconnect.

Manages the Bluetooth connection between the Raspberry Pi and the
OBDLink MX+ dongle via /dev/rfcomm0. Handles:

    - Initial connection with Pi-specific settings (fast=False, timeout=30).
      The fast=False flag is required on Pi — without it, python-obd sends
      an AT command that causes the Pi Bluetooth stack to drop the connection.

    - Retry loop on connection failure — the OBDLink may not be powered on
      immediately when the Pi boots (ignition delay), so retries every 15s
      indefinitely until the engine is running and the dongle is ready.

    - Mid-trip reconnect — if Bluetooth drops during a drive, the collector
      calls reconnect() which re-establishes the connection and continues
      the current trip (same trip_id, gap in data).

    - Reconnect count tracking — reported in every Pi health sync payload
      so the server can alert if BT reliability degrades over time.
"""

import time

import obd

from config import config
from logger import logger

RETRY_INTERVAL_S = 15


class OBDConnection:
    """Manages the lifecycle of the OBD-II Bluetooth connection.

    Attributes:
        reconnect_count: Number of mid-trip reconnections since last boot.
                         Included in every Pi health sync payload.
    """

    def __init__(self) -> None:
        self._connection: obd.OBD | None = None
        self.reconnect_count: int = 0

    def connect(self) -> None:
        """Connect to OBDLink MX+ with indefinite retry.

        Blocks until a connection is established. Retries every
        RETRY_INTERVAL_S seconds on failure — the dongle may not be
        powered on immediately at Pi boot (ignition delay).

        Logs each retry attempt and logs success with the device name.
        """
        # TODO: Task 8 — implement connection loop with fast=False, timeout=30
        pass

    def reconnect(self) -> None:
        """Re-establish connection after a mid-trip Bluetooth drop.

        Increments reconnect_count so the health payload can surface
        BT reliability issues on the Grafana dashboard.
        """
        # TODO: Task 8 — disconnect cleanly, reconnect, increment reconnect_count
        pass

    def disconnect(self) -> None:
        """Close the OBD connection cleanly on shutdown."""
        # TODO: Task 8 — close connection if open, log disconnect
        pass

    @property
    def is_connected(self) -> bool:
        """Return True if the OBD connection is currently active."""
        return self._connection is not None and self._connection.is_connected()

    @property
    def connection(self) -> obd.OBD | None:
        """Return the underlying python-obd connection object."""
        return self._connection
