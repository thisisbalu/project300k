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

        Logs each retry attempt with the attempt number and logs success
        with the connected port name.
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                logger.info(f"OBD connection attempt {attempt} on {config.OBD_PORT}")

                # fast=False is required on Pi — the default AT fast init
                # command causes the Pi Bluetooth stack to drop the connection.
                # timeout=30 gives the OBDLink time to respond over BT.
                conn = obd.OBD(config.OBD_PORT, fast=False, timeout=30)

                if conn.is_connected():
                    self._connection = conn
                    logger.info(
                        f"OBD connection established — port: {config.OBD_PORT} "
                        f"protocol: {conn.protocol_name()}"
                    )
                    return

                # python-obd can return a connection object without raising
                # an exception even when the connection failed — check explicitly.
                conn.close()
                raise ConnectionError("Connection object returned but is_connected() is False")

            except Exception as e:
                logger.warning(
                    f"OBD connection attempt {attempt} failed: {e} — "
                    f"retrying in {RETRY_INTERVAL_S}s"
                )
                time.sleep(RETRY_INTERVAL_S)

    def reconnect(self) -> None:
        """Re-establish connection after a mid-trip Bluetooth drop.

        Disconnects cleanly first to ensure the rfcomm socket is released,
        then calls connect() which retries until the dongle responds.
        Increments reconnect_count so the health payload surfaces BT
        reliability issues on the Grafana dashboard.
        """
        logger.warning("OBD connection lost — attempting reconnect")
        self.disconnect()
        self.reconnect_count += 1
        self.connect()
        logger.info(f"OBD reconnected (total reconnects this session: {self.reconnect_count})")

    def disconnect(self) -> None:
        """Close the OBD connection cleanly on shutdown or before reconnect."""
        if self._connection is not None:
            try:
                self._connection.close()
                logger.info("OBD connection closed")
            except Exception as e:
                # Log but do not raise — disconnect is best-effort.
                # The rfcomm socket will be released when the process exits.
                logger.warning(f"Error closing OBD connection: {e}")
            finally:
                self._connection = None

    @property
    def is_connected(self) -> bool:
        """Return True if the OBD connection is currently active."""
        return self._connection is not None and self._connection.is_connected()

    @property
    def connection(self) -> obd.OBD | None:
        """Return the underlying python-obd connection object."""
        return self._connection
