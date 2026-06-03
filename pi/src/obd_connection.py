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

from __future__ import annotations

import threading
import time
from typing import Callable

import obd

from config import config
from health import write_reconnect_count
from logger import logger

RETRY_INTERVAL_S = 15
_CONNECT_TIMEOUT_S = 60


def connect_with_timeout(
    factory: Callable[[], obd.OBD], timeout_s: int = _CONNECT_TIMEOUT_S
) -> obd.OBD:
    """Run a python-obd connection constructor in a daemon thread with a hard timeout.

    python-obd can hang indefinitely on serial reads when rfcomm0 exists but the
    underlying Bluetooth link has dropped — the serial read timeout does not cover
    every code path in obd.OBD.__init__ (nor obd.Async, which subclasses it).
    Without this wrapper the caller blocks until systemd's TimeoutStartSec/watchdog
    kills the process. Used for both the sync obd.OBD verify and the collector's
    obd.Async open so the guard lives in one place.

    If the constructor completes *after* the timeout, the late connection is closed
    so it does not leak /dev/rfcomm0 and contend with the next attempt.

    Args:
        factory:   Zero-arg callable returning a connected obd.OBD/obd.Async.
        timeout_s: Hard wall-clock limit before giving up on the constructor.

    Raises:
        ConnectionError: If the constructor does not return within timeout_s.
        Exception:       Re-raised from the constructor if it failed in time.
    """
    box: dict = {"conn": None, "exc": None}
    lock = threading.Lock()
    timed_out = False

    def _run() -> None:
        conn = None
        exc = None
        try:
            conn = factory()
        except BaseException as e:
            # Marshal any exception (including KeyboardInterrupt) back to the
            # caller's thread — otherwise it dies silently here and connect()
            # never sees it, spinning its retry loop forever.
            exc = e
        with lock:
            if timed_out:
                # Caller already gave up — release the port so the next attempt
                # does not contend with an abandoned, never-closed connection.
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                return
            box["conn"], box["exc"] = conn, exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=timeout_s)

    with lock:
        # Worker may have finished right at the deadline — prefer its result.
        if box["conn"] is not None or box["exc"] is not None:
            if box["exc"] is not None:
                raise box["exc"]
            return box["conn"]
        # Still running and no result yet — give up under the lock so a late
        # completion sees timed_out and closes the connection it produced.
        timed_out = True

    raise ConnectionError(
        f"OBD connect hung for {timeout_s}s — rfcomm0 may be stale, "
        "waiting for rfcomm-connect to re-establish the link"
    )


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
        """Verify OBDLink MX+ is reachable then release the connection.

        Blocks with indefinite retry until the dongle responds — the dongle
        may not be powered on immediately at Pi boot (ignition delay). Once
        verified, the connection is closed immediately so Collector can open
        obd.Async on the same /dev/rfcomm0 port without conflict.

        Keeping the sync OBD connection open alongside the async connection
        would cause both to hold /dev/rfcomm0 simultaneously, splitting the
        serial byte stream between them and making DTC queries unreliable.
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                logger.info(f"OBD connection attempt {attempt} on {config.OBD_PORT}")

                conn = connect_with_timeout(
                    lambda: obd.OBD(config.OBD_PORT, fast=False, timeout=30)
                )

                if conn.is_connected():
                    protocol = conn.protocol_name()
                    # Close immediately — Collector.query_sync() issues all
                    # synchronous queries through the async connection after
                    # temporarily stopping the polling loop.
                    conn.close()
                    self._connection = None
                    logger.info(
                        f"OBD dongle verified on {config.OBD_PORT} "
                        f"protocol: {protocol}"
                    )
                    return

                conn.close()
                raise ConnectionError("Connection object returned but is_connected() is False")

            except KeyboardInterrupt:
                raise
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
        # Persist count so the sync script (separate process) can include
        # the live value in health snapshots without inter-process communication.
        write_reconnect_count(self.reconnect_count)
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
