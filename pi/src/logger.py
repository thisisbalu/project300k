"""
logger.py — Shared rotating file logger for the OBD collector.

Provides a single `logger` instance imported by every other module.
Writes to a rotating log file on the USB drive (5MB × 7 files = 35MB cap).
Falls back to stderr-only if the log file cannot be opened — this handles
the case where the USB drive is not yet mounted during early boot.

Log levels:
    File handler    — DEBUG and above (all events)
    Console/stderr  — WARNING and above (captured by systemd journal)

Log format:
    2026-05-29T08:00:01 | INFO     | Trip started: abc-123
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from config import config


def _build_logger() -> logging.Logger:
    """Construct and configure the logger instance.

    Called once at module import time. Subsequent imports receive the
    same Logger object from Python's logging registry.

    Returns:
        Configured Logger instance named 'obd-collector'.
    """
    log = logging.getLogger("obd-collector")
    log.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    # File handler — all levels, rotating 5MB × 7 files.
    # Wrapped in try/except because the USB drive may not be mounted yet
    # on the first boot or if the drive is removed. Logging failure must
    # never crash the collector — it degrades to stderr only.
    try:
        log_dir = os.path.dirname(config.LOG_PATH)
        # Only create the log directory if the USB drive is mounted.
        # Without this check, os.makedirs would silently create /mnt/usb/logs/
        # on the SD card's root filesystem when the USB drive is not yet mounted,
        # causing subsequent boots to write logs to the SD card instead of flagging
        # the missing drive.
        if not os.path.ismount("/mnt/usb"):
            raise OSError("USB drive not mounted at /mnt/usb")
        os.makedirs(log_dir, exist_ok=True)
        file_handler = RotatingFileHandler(
            filename=config.LOG_PATH,
            maxBytes=5 * 1024 * 1024,
            backupCount=7,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        log.addHandler(file_handler)
    except OSError as e:
        print(
            f"WARNING | Could not open log file {config.LOG_PATH}: {e} — logging to stderr only",
            file=sys.stderr,
        )

    # Console/stderr handler — WARNING and above only.
    # systemd captures stderr and writes it to the journal, so this gives
    # visibility into warnings and errors without flooding the journal with
    # every DEBUG line from the polling loop.
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(formatter)
    log.addHandler(console_handler)

    log.info(f"Pi boot — Python {sys.version.split()[0]} — log: {config.LOG_PATH}")

    return log


logger = _build_logger()
