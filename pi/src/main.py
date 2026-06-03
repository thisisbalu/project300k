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

import psutil
import sdnotify
import health
from collector import Collector
from config import config
from logger import init_file_logging, logger
from obd_connection import OBDConnection
from queue_writer import QueueWriter
from storage import get_connection, init_schema
from trip import TripManager


# When dtoverlay=i2c-rtc,ds3231 is active the kernel rtc-ds1307 driver claims
# the I2C address — direct smbus2 access is blocked. Check via sysfs instead.
_RTC_NAME_PATH = "/sys/class/rtc/rtc0/name"


def check_rtc() -> int:
    """Check DS3231 presence via sysfs and log a warning if not found.

    The kernel rtc-ds1307 driver claims the DS3231's I2C address once
    dtoverlay=i2c-rtc,ds3231 is active, so direct smbus2 access is blocked.
    Sysfs exposes the driver name at /sys/class/rtc/rtc0/name — if it contains
    'ds1307' the chip is present and the kernel has initialised it successfully.

    Non-fatal — the collector continues regardless.

    Returns:
        1 if DS3231 is present (kernel driver loaded it successfully).
        0 if rtc0 is missing or is an unexpected driver.
    """
    try:
        with open(_RTC_NAME_PATH) as f:
            name = f.read().strip()
        if "ds1307" not in name:
            logger.warning(
                f"Unexpected RTC driver '{name}' at rtc0 — expected ds1307 (DS3231). "
                "Timestamps may be unreliable."
            )
            return 0
        logger.info("DS3231 RTC OK — clock is reliable")
        return 1
    except OSError:
        logger.warning(
            "DS3231 not found — /sys/class/rtc/rtc0 not present. "
            "Check wiring and raspi-config I2C setting. Relying on fake-hwclock for timestamps."
        )
        return 0


def _log_heartbeat(collector, uptime_start: float) -> None:
    """Log a 5-minute heartbeat with Pi vitals and polling health."""
    uptime_s = int(time.monotonic() - uptime_start)
    uptime_str = f"{uptime_s // 60}m {uptime_s % 60}s"

    try:
        with open(health.CPU_TEMP_PATH) as f:
            cpu_str = f"{int(f.read().strip()) / 1000.0:.1f}°C"
    except OSError:
        cpu_str = "N/A"

    mem = psutil.virtual_memory()
    mem_str = f"{mem.available // (1024 * 1024)}/{mem.total // (1024 * 1024)}MB"

    try:
        disk = psutil.disk_usage("/mnt/usb")
        disk_str = f"{disk.free / (1024 ** 3):.1f}/{disk.total / (1024 ** 3):.1f}GB"
    except OSError:
        disk_str = "N/A"

    rpm = collector.latest("rpm")
    speed = collector.latest("speed_kmh")
    rpm_str = str(int(rpm)) if rpm is not None else "N/A"
    speed_str = str(int(speed)) if speed is not None else "N/A"

    active, total = collector.polling_health(window_s=60)

    logger.info(
        f"Heartbeat — uptime: {uptime_str} | cpu: {cpu_str} | "
        f"mem: {mem_str} | disk: {disk_str} | "
        f"rpm: {rpm_str} | speed: {speed_str}km/h | "
        f"pids: {active}/{total}"
    )


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

    # Attach the rotating USB file handler (collector-only) before anything logs.
    init_file_logging()

    # Initialise sdnotify early — used for both READY and WATCHDOG signals.
    notifier = sdnotify.SystemdNotifier()

    logger.info("Starting obd-collector")
    logger.info(f"Config loaded: {config}")

    rtc_ok = check_rtc()
    health.write_rtc_ok(rtc_ok)
    restart_count = health.increment_restart_count()

    conn = get_connection()
    init_schema(conn)

    queue_writer = QueueWriter(conn)
    queue_writer.start()

    obd_connection = OBDConnection()
    obd_connection.connect()

    trip_manager = TripManager(queue_writer)
    # Collector opens its own obd.Async connection — obd.Async is a subclass
    # of obd.OBD and must be instantiated with a port string, not wrapped
    # around the existing OBDConnection object.
    # obd_connection is passed for reconnect() and reconnect_count tracking.
    collector = Collector(queue_writer, trip_manager, obd_connection)
    # Wire DTC query callable before start() — TripManager calls query_sync()
    # for DTC scans at trip boundaries, which stops the async loop to avoid
    # byte-race contention on /dev/rfcomm0 with the polling thread.
    trip_manager.set_dtc_query(collector.query_sync)
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
    _heartbeat_ticks = 0
    _uptime_start = time.monotonic()
    try:
        while True:
            # A live main loop is not enough: data collection runs on the
            # queue-writer drain thread and BT recovery on the obd-monitor thread.
            # If either has died, withhold the watchdog ping and exit so systemd
            # (Restart=always) restarts a clean process — otherwise we would keep
            # pinging the watchdog as a zombie that silently collects nothing.
            if not queue_writer.is_alive:
                logger.error("Queue-writer thread dead — exiting for systemd restart")
                break
            if not collector.is_monitor_alive():
                logger.error("OBD monitor thread dead — exiting for systemd restart")
                break

            notifier.notify("WATCHDOG=1")
            _heartbeat_ticks += 1
            if _heartbeat_ticks % 2 == 0:
                _log_heartbeat(collector, _uptime_start)
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
