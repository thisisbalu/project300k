"""
health.py — Pi health metrics collection for the sync payload.

Every sync to the home server includes a Pi health snapshot. This lets
the Grafana dashboard surface Pi-side issues (disk filling up, high CPU
temp, frequent OBD reconnects) without needing to SSH into the car.

Metrics collected:
    cpu_temp_c          — CPU temperature from kernel thermal interface
    memory_free_mb      — Available RAM (includes reclaimable cache)
    disk_free_mb        — Free space on /mnt/usb (the data drive)
    obd_reconnect_count — Mid-trip BT reconnections since last boot
    restart_count       — Total collector script restarts (persistent file)
    last_error          — Last ERROR line from the log file
    rows_collected      — Rows written to SQLite since last sync

restart_count is stored in a plain text file on the USB drive so it
survives across systemd restarts and Pi reboots. It is incremented on
every boot of main.py before anything else runs.
"""

import os

import psutil

from config import config
from logger import logger


RESTART_COUNT_PATH = "/mnt/usb/data/restart_count"
CPU_TEMP_PATH = "/sys/class/thermal/thermal_zone0/temp"


def increment_restart_count() -> int:
    """Read, increment, and persist the restart counter.

    Called once at the start of main() on every boot. The counter
    survives reboots because it is stored on the USB drive, not in RAM.

    Falls back to 0 if the file does not exist yet (first ever boot)
    or cannot be read. Write failures are logged but not fatal.

    Returns:
        The new restart count after incrementing.
    """
    count = 0
    try:
        if os.path.exists(RESTART_COUNT_PATH):
            with open(RESTART_COUNT_PATH) as f:
                count = int(f.read().strip())
    except (ValueError, OSError) as e:
        logger.warning(f"Could not read restart count: {e} — starting from 0")

    count += 1

    try:
        with open(RESTART_COUNT_PATH, "w") as f:
            f.write(str(count))
    except OSError as e:
        logger.warning(f"Could not write restart count: {e}")

    logger.info(f"Restart count: {count}")
    return count


def _read_cpu_temp() -> float | None:
    """Read CPU temperature from the kernel thermal interface.

    Returns temperature in Celsius, or None if the file is unavailable.
    The raw value is in millidegrees Celsius and must be divided by 1000.
    """
    try:
        with open(CPU_TEMP_PATH) as f:
            return int(f.read().strip()) / 1000.0
    except OSError:
        return None


def _read_last_error() -> str | None:
    """Read the last ERROR line from the log file.

    Scans the current log file in reverse to find the most recent ERROR
    entry. Returns None if no error has been logged or log is unavailable.
    """
    try:
        with open(config.LOG_PATH) as f:
            lines = f.readlines()
        # Scan from the end — most recent error is what matters.
        for line in reversed(lines):
            if "| ERROR" in line:
                return line.strip()
    except OSError:
        pass
    return None


def collect(obd_reconnect_count: int, restart_count: int) -> dict:
    """Collect a Pi health snapshot for the sync payload.

    Args:
        obd_reconnect_count: From OBDConnection.reconnect_count.
        restart_count:       Current value from increment_restart_count().

    Returns:
        Dict with keys matching the pi_health_log table columns.
    """
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/mnt/usb")

    return {
        "cpu_temp_c": _read_cpu_temp(),
        "memory_free_mb": round(mem.available / 1024 / 1024, 1),
        "disk_free_mb": round(disk.free / 1024 / 1024, 1),
        "obd_reconnect_count": obd_reconnect_count,
        "restart_count": restart_count,
        "last_error": _read_last_error(),
    }
