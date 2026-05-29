"""
main.py — Entry point for the OBD collector service.

Bootstraps all components in dependency order and runs the main loop.
Managed by obd-collector.service (systemd) with Restart=always so the
service recovers automatically from any unhandled exception.

Boot sequence:
    1. Load config (exits immediately on missing required values)
    2. Initialise logger (falls back to stderr if USB drive not mounted)
    3. Check DS3231 RTC — log WARNING if battery dead or chip not found
    4. Increment persistent restart counter on USB drive
    5. Open SQLite connection and initialise schema
    6. Start QueueWriter (background thread)
    7. Connect to OBDLink MX+ (retries every 15s until success)
    8. Start TripManager and Collector
    9. Enter watchdog ping loop (pings systemd every 30s)

Shutdown (SIGTERM or KeyboardInterrupt):
    Collector and QueueWriter are stopped cleanly — the queue is drained
    before the SQLite connection is closed so no in-flight rows are lost.
"""

import sys
import time

import health
from collector import Collector
from config import config
from logger import logger
from obd_connection import OBDConnection
from queue_writer import QueueWriter
from storage import get_connection, init_schema
from trip import TripManager


def check_rtc() -> None:
    """Read the DS3231 RTC OSF flag and log a warning if the battery is dead.

    The OSF (Oscillator Stop Flag) is set by the DS3231 when power to the
    RTC was interrupted — indicating the coin cell battery is dead or missing.
    When this happens, the RTC time is unreliable and fake-hwclock takes over
    as the fallback clock source.

    Non-fatal — the collector continues regardless. The warning in the log
    alerts the operator to replace the CR2032 coin cell.
    """
    # TODO: Task 4 — read DS3231 OSF flag via I2C, log WARNING if set or chip not found
    pass


def main() -> None:
    """Bootstrap all components and run the main watchdog loop."""
    logger.info("Starting obd-collector")
    logger.info(f"Config loaded: {config}")

    check_rtc()
    health.increment_restart_count()

    conn = get_connection()
    init_schema(conn)

    queue_writer = QueueWriter(conn)
    queue_writer.start()

    obd_connection = OBDConnection()
    obd_connection.connect()

    trip_manager = TripManager(queue_writer, obd_connection)
    collector = Collector(obd_connection, queue_writer, trip_manager)
    collector.start()

    # Main loop — pings systemd watchdog every 30s.
    # If this loop stalls (deadlock, infinite block), the watchdog times out
    # after WatchdogSec=60s and systemd restarts the service automatically.
    # TODO: Task 15 — add sdnotify watchdog ping
    try:
        while True:
            time.sleep(30)
    except KeyboardInterrupt:
        logger.info("Shutting down — draining queue")
    finally:
        collector.stop()
        queue_writer.stop()  # blocks until queue is empty
        obd_connection.disconnect()
        conn.close()
        logger.info("obd-collector stopped")


if __name__ == "__main__":
    main()
