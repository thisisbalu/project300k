"""
health.py — Pi health metrics collection for the sync payload.

Every sync to the home server includes a Pi health snapshot. This lets
the Grafana dashboard surface Pi-side issues (disk filling up, high CPU
temp, frequent OBD reconnects) without needing to SSH into the car.

Metrics collected:
    cpu_temp_c          — CPU temperature from kernel thermal interface
    memory_free_mb      — Available RAM (not just free — includes cache)
    disk_free_mb        — Free space on /mnt/usb (the data drive)
    obd_reconnect_count — Mid-trip BT reconnections since last boot
    restart_count       — Total collector script restarts (persistent)
    last_error          — Last ERROR line from the log file
    rows_collected      — Rows written to SQLite since last sync

restart_count is stored in a plain text file on the USB drive so it
survives across systemd restarts and Pi reboots. It is incremented on
every boot of main.py before anything else runs.
"""

from logger import logger
from config import config


RESTART_COUNT_PATH = "/mnt/usb/data/restart_count"


def increment_restart_count() -> int:
    """Read, increment, and persist the restart counter.

    Called once at the start of main() on every boot. The counter
    survives reboots because it is stored on the USB drive, not in RAM.

    Returns:
        The new restart count after incrementing.
    """
    # TODO: Task 12 — read counter file, increment, write back, return new value
    pass


def collect() -> dict:
    """Collect a Pi health snapshot for the sync payload.

    Returns:
        Dict with keys matching the pi_health_log table columns:
            cpu_temp_c, memory_free_mb, disk_free_mb,
            obd_reconnect_count, restart_count,
            last_error, rows_collected
    """
    # TODO: Task 12 — read CPU temp, memory, disk, reconnect count, last error, rows since sync
    pass
