"""
health.py — Pi health metrics collection for the sync payload.

Every sync to the home server includes a Pi health snapshot. This lets
the Grafana dashboard surface Pi-side issues (disk filling up, high CPU
temp, frequent OBD reconnects) without needing to SSH into the car.

Metrics collected:
    cpu_temp_c          — CPU temperature from kernel thermal interface
    cpu_usage_pct       — CPU utilisation percentage
    memory_free_mb      — Available RAM (includes reclaimable cache)
    disk_free_mb        — Free space on /mnt/usb (the data drive)
    uptime_s            — Seconds since last Pi boot
    usb_drive_mounted   — 1 if /mnt/usb is accessible, 0 if not
    bt_adapter_present  — 1 if hci0 (USB BT dongle) is detected, 0 if not
    obd_reconnect_count — Mid-trip BT reconnections since last boot
    restart_count       — Total collector script restarts (persistent file)
    rtc_ok              — 1 if DS3231 OSF clear and chip found, 0 otherwise
    last_error          — Last ERROR line from the log file
    rows_collected      — Unsynced rows at snapshot time
    collector_version   — Python collector version string

restart_count is stored in a plain text file on the USB drive so it
survives across systemd restarts and Pi reboots. It is incremented on
every boot of main.py before anything else runs.

obd_reconnect_count is also stored in a file so the sync script (which
runs as a separate process) can include the live value in health snapshots.
The collector updates it every time OBDConnection.reconnect() is called.
"""

from __future__ import annotations

import os
import time

import psutil

from config import config
from logger import logger


RESTART_COUNT_PATH   = "/mnt/usb/data/restart_count"
RECONNECT_COUNT_PATH = "/mnt/usb/data/reconnect_count"
RTC_OK_PATH          = "/mnt/usb/data/rtc_ok"
CPU_TEMP_PATH        = "/sys/class/thermal/thermal_zone0/temp"
USB_MOUNT_PATH       = "/mnt/usb"
BT_ADAPTER_PATH      = "/sys/class/bluetooth/hci0"
VERSION_PATH         = "/home/balu/project300k/pi/VERSION"


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
            # fsync so the count survives an abrupt power cut (engine off).
            # Without this, the write may sit in the kernel page cache and be
            # lost if power is cut before the cache is flushed to the USB drive.
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        logger.warning(f"Could not write restart count: {e}")

    logger.info(f"Restart count: {count}")
    return count


def read_reconnect_count() -> int:
    """Read the OBD BT reconnect count written by the collector process.

    The sync script runs as a separate process and cannot access
    OBDConnection.reconnect_count directly. The collector writes the count
    to a file on every reconnect; this function reads it for the health snapshot.

    Returns 0 if the file is missing (first boot or collector not running).
    """
    try:
        if os.path.exists(RECONNECT_COUNT_PATH):
            with open(RECONNECT_COUNT_PATH) as f:
                return int(f.read().strip())
    except (ValueError, OSError):
        pass
    return 0


def write_rtc_ok(ok: int) -> None:
    """Persist the RTC OSF check result so the sync script can report it.

    Called once in main.py after check_rtc(). The sync script runs as a
    separate process and cannot access the boot-time RTC result from memory.
    Without this file, every health snapshot would default to rtc_ok=1 even
    when the DS3231 coin cell is dead.

    Write failures are logged but not fatal — rtc_ok is best-effort.
    """
    try:
        with open(RTC_OK_PATH, "w") as f:
            f.write(str(ok))
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        logger.warning(f"Could not write rtc_ok: {e}")


def read_rtc_ok() -> int:
    """Read the RTC OSF result written by main.py on boot.

    Returns 1 (clock assumed OK) if the file is missing — this is the safe
    default because a missing file means either first boot (USB not yet
    mounted) or the write failed, not a confirmed clock failure.
    """
    try:
        if os.path.exists(RTC_OK_PATH):
            with open(RTC_OK_PATH) as f:
                return int(f.read().strip())
    except (ValueError, OSError):
        pass
    return 1


def read_restart_count() -> int:
    """Read the collector restart count written by main.py on every boot.

    The sync script runs as a separate process and cannot access the in-memory
    count. main.py calls increment_restart_count() on boot which persists the
    value; this function reads it without incrementing for health snapshots.

    Returns 0 if the file is missing (first boot or USB not mounted).
    """
    try:
        if os.path.exists(RESTART_COUNT_PATH):
            with open(RESTART_COUNT_PATH) as f:
                return int(f.read().strip())
    except (ValueError, OSError):
        pass
    return 0


def write_reconnect_count(count: int) -> None:
    """Persist the OBD BT reconnect count to the USB drive.

    Called by OBDConnection.reconnect() so the sync script can include
    the live count in health snapshots without inter-process communication.

    Write failures are logged but not fatal — the count is best-effort.
    """
    try:
        with open(RECONNECT_COUNT_PATH, "w") as f:
            f.write(str(count))
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        logger.warning(f"Could not write reconnect count: {e}")


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

    Reads only the last 64KB of the log file rather than loading the entire
    file (up to 5MB) into memory. On a Pi 3B with 1GB RAM, 5MB per sync
    invocation is wasteful — 64KB is more than enough to find the last error.

    Returns None if no error has been logged or log is unavailable.
    """
    try:
        with open(config.LOG_PATH, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            # Read last 64KB — sufficient to find the most recent ERROR line
            # without loading the full rotating log file into memory.
            f.seek(max(0, size - 65536))
            tail = f.read().decode("utf-8", errors="replace")
        for line in reversed(tail.splitlines()):
            if "| ERROR" in line:
                return line.strip()
    except OSError:
        pass
    return None


def _read_collector_version() -> str:
    """Read the collector version string from the VERSION file.

    Returns 'unknown' if the file is missing — version file is created
    as part of the release process.
    """
    try:
        with open(VERSION_PATH) as f:
            return f.read().strip()
    except OSError:
        return "unknown"


def _check_usb_mounted() -> int:
    """Check if the USB flash drive is mounted and accessible.

    Returns 1 if /mnt/usb is a mount point, 0 otherwise.
    A 0 here means SQLite data is not being written to the drive.
    """
    return 1 if os.path.ismount(USB_MOUNT_PATH) else 0


def _check_bt_adapter() -> int:
    """Check if the USB Bluetooth adapter (hci0) is present.

    Returns 1 if the sysfs path for hci0 exists, 0 otherwise.
    A 0 here means the USB BT dongle is missing or not recognised —
    OBD connection will fail until it is detected.
    """
    return 1 if os.path.exists(BT_ADAPTER_PATH) else 0


def _read_uptime() -> int:
    """Return seconds since the Pi last booted.

    Uses psutil.boot_time() which reads from /proc/uptime internally.
    """
    return int(time.time() - psutil.boot_time())


def collect(
    obd_reconnect_count: int,
    restart_count: int,
    rtc_ok: int = 1,
) -> dict:
    """Collect a full Pi health snapshot for the sync payload.

    Args:
        obd_reconnect_count: From OBDConnection.reconnect_count (collector)
                             or read_reconnect_count() (sync script).
        restart_count:       Current value from increment_restart_count().
        rtc_ok:              1 if DS3231 OSF clear and chip found, 0 otherwise.
                             Passed in from check_rtc() in main.py.

    Returns:
        Dict with keys matching the pi_health_log table columns.
    """
    mem        = psutil.virtual_memory()
    usb_mounted = _check_usb_mounted()   # call once, reuse below
    disk       = psutil.disk_usage(USB_MOUNT_PATH) if usb_mounted else None

    # cpu_percent(interval=None) returns usage since last call — non-blocking.
    # The first call ever returns 0.0 (no prior measurement), which is acceptable
    # at boot. Subsequent calls return an accurate reading without blocking 1s.
    cpu_usage = psutil.cpu_percent(interval=None)

    return {
        "cpu_temp_c":          _read_cpu_temp(),
        "cpu_usage_pct":       cpu_usage,
        "memory_free_mb":      round(mem.available / 1024 / 1024, 1),
        "disk_free_mb":        round(disk.free / 1024 / 1024, 1) if disk else None,
        "uptime_s":            _read_uptime(),
        "usb_drive_mounted":   usb_mounted,
        "bt_adapter_present":  _check_bt_adapter(),
        "obd_reconnect_count": obd_reconnect_count,
        "restart_count":       restart_count,
        "rtc_ok":              rtc_ok,
        "last_error":          _read_last_error(),
        "collector_version":   _read_collector_version(),
    }
