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

import signal
import sys
import time

import sdnotify
import smbus2
import health
from collector import Collector
from config import config
from logger import logger
from obd_connection import OBDConnection
from queue_writer import QueueWriter
from storage import get_connection, init_schema
from trip import TripManager


# DS3231 I2C constants
_DS3231_I2C_ADDRESS = 0x68   # fixed hardware address, not configurable
_DS3231_STATUS_REG  = 0x0F   # status register contains the OSF flag
_DS3231_OSF_BIT     = 0x80   # bit 7 of status register — Oscillator Stop Flag
_I2C_BUS            = 1      # I2C bus 1 on Pi 3B (GPIO pins 2 and 3)


def check_rtc() -> int:
    """Read the DS3231 RTC OSF flag and log a warning if the battery is dead.

    The OSF (Oscillator Stop Flag) is set by the DS3231 when power to the
    RTC was interrupted — indicating the coin cell battery is dead or missing.
    When OSF is set, the RTC time since last power loss is unreliable and
    fake-hwclock takes over as the fallback clock source.

    Non-fatal — the collector continues regardless. The warning in the log
    alerts the operator to replace the CR2032 coin cell (every 3 years).

    Two warning conditions:
        OSF set     — chip found but battery lost power; timestamps may be wrong
        OSError     — chip not responding on I2C bus (not wired or not enabled)

    Returns:
        1 if DS3231 is present and OSF is clear (clock reliable).
        0 if OSF is set or chip not found.
    """
    try:
        # Context manager guarantees bus.close() on any exit path —
        # including if read_byte_data() raises, which would skip close()
        # if the bus were opened manually.
        with smbus2.SMBus(_I2C_BUS) as bus:
            status = bus.read_byte_data(_DS3231_I2C_ADDRESS, _DS3231_STATUS_REG)

        if status & _DS3231_OSF_BIT:
            # OSF is set — RTC lost power at some point since last cleared.
            # This typically means the CR2032 coin cell is dead or was never
            # installed. fake-hwclock will provide an approximate time instead.
            logger.warning(
                "DS3231 OSF flag set — RTC battery may be dead, timestamp accuracy "
                "not guaranteed. Replace CR2032 coin cell."
            )
            return 0

        logger.info("DS3231 RTC OK — OSF clear, clock is reliable")
        return 1

    except OSError:
        # I2C address 0x68 did not respond — DS3231 not wired, not enabled
        # in raspi-config, or I2C interface not loaded. fake-hwclock is the
        # only clock source in this case.
        logger.warning(
            "DS3231 not found on I2C bus — check wiring and raspi-config I2C setting. "
            "Relying on fake-hwclock for timestamps."
        )
        return 0


def _handle_sigterm(sig, frame) -> None:
    """Convert SIGTERM to KeyboardInterrupt so the finally block runs.

    Python only raises KeyboardInterrupt automatically on SIGINT (Ctrl-C).
    SIGTERM (sent by systemd on 'systemctl stop') does not trigger it by
    default — the process is killed abruptly, skipping the finally block
    and leaving the queue undrained and the SQLite connection unclosed.
    This handler converts SIGTERM to KeyboardInterrupt so the main loop's
    try/finally always runs on both signals.
    """
    raise KeyboardInterrupt


def main() -> None:
    """Bootstrap all components and run the main watchdog loop."""
    # Register SIGTERM handler before anything else — systemd sends SIGTERM
    # to stop the service and we must drain the queue and close SQLite cleanly.
    signal.signal(signal.SIGTERM, _handle_sigterm)

    # Initialise sdnotify early — used for both READY and WATCHDOG signals.
    notifier = sdnotify.SystemdNotifier()

    logger.info("Starting obd-collector")
    logger.info(f"Config loaded: {config}")

    rtc_ok = check_rtc()
    restart_count = health.increment_restart_count()

    conn = get_connection()
    init_schema(conn)

    queue_writer = QueueWriter(conn)
    queue_writer.start()

    obd_connection = OBDConnection()
    obd_connection.connect()

    trip_manager = TripManager(queue_writer, obd_connection)
    # Collector opens its own obd.Async connection — obd.Async is a subclass
    # of obd.OBD and must be instantiated with a port string, not wrapped
    # around the existing OBDConnection object.
    # obd_connection is passed for reconnect() and reconnect_count tracking.
    collector = Collector(queue_writer, trip_manager, obd_connection)
    collector.start()

    # Notify systemd that initialisation is complete and the service is ready.
    # Required because obd-collector.service uses Type=notify — systemd waits
    # for this signal before marking the service as active or starting dependents.
    notifier.notify("READY=1")
    logger.info("obd-collector ready")

    # Main watchdog loop — pings systemd every 30s.
    # WatchdogSec=60s in the service file — if no ping arrives within 60s,
    # systemd kills and restarts the service. The 30s ping interval gives
    # a 2x safety margin so a single delayed iteration never triggers a restart.
    # If this loop stalls (deadlock, hung thread), the watchdog fires correctly.
    try:
        while True:
            notifier.notify("WATCHDOG=1")
            logger.info("Watchdog ping sent")
            time.sleep(30)
    except KeyboardInterrupt:
        logger.info("Shutting down — draining queue")
    finally:
        collector.stop()
        # Join DTC scan threads before disconnecting — they hold a reference
        # to obd_connection.connection and call query() on it.
        trip_manager.stop()
        queue_writer.stop()  # blocks until queue is empty
        obd_connection.disconnect()
        conn.close()
        logger.info("obd-collector stopped")


if __name__ == "__main__":  # pragma: no cover
    main()
