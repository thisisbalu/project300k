"""
logger.py — Shared logger for the OBD collector.

Provides a single `logger` instance imported by every module. The stderr
handler (captured by systemd's journal) is attached at import so the logger
always works. The rotating USB file handler is attached explicitly by the
collector via init_file_logging() — the sync process deliberately does NOT
attach it (see configure_sync_logging) because RotatingFileHandler is not
multi-process safe and the collector and sync run as separate processes;
sharing one file would corrupt rotation and lose lines.

Log levels:
    File handler (collector only) — DEBUG and above
    stderr handler                — WARNING+ (collector) / INFO+ (sync), to journald

Timestamps are UTC to match the ISO8601 UTC timestamps stored in SQLite.

Log format:
    2026-05-29T08:00:01Z | INFO     | Trip started: abc-123
"""

import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler

from config import config

_FORMATTER = logging.Formatter(
    fmt="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
# Log in UTC so log timestamps line up with the ISO8601 UTC timestamps written
# to SQLite — correlating a log line with a data row needs no tz conversion.
_FORMATTER.converter = time.gmtime


logger = logging.getLogger("obd-collector")
logger.setLevel(logging.DEBUG)

# stderr handler is always present so the logger works in any process and is
# captured by journald. WARNING+ by default (collector); sync raises it to INFO
# via configure_sync_logging() so its progress is visible in the journal.
_stderr_handler = logging.StreamHandler(sys.stderr)
_stderr_handler.setLevel(logging.WARNING)
_stderr_handler.setFormatter(_FORMATTER)
logger.addHandler(_stderr_handler)


def init_file_logging() -> None:
    """Attach the rotating USB file handler — collector only.

    Called once by main.py at startup. The sync process must NOT call this: it
    logs to stderr→journald instead, so the collector and sync never share a
    single RotatingFileHandler (not multi-process safe — when one process rolls
    the file the other holds open, rotation breaks and lines are lost).

    Degrades to stderr-only if the USB drive is not mounted — logging failure
    must never crash the collector. Also emits the boot marker line.
    """
    try:
        log_dir = os.path.dirname(config.LOG_PATH)
        # Only create the log directory if the USB drive is mounted. Without this
        # check, os.makedirs would silently create /mnt/usb/logs/ on the SD card's
        # root filesystem when the USB drive is not yet mounted, hiding the missing
        # drive and writing logs to the SD card instead of flagging the problem.
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
        file_handler.setFormatter(_FORMATTER)
        logger.addHandler(file_handler)
    except OSError as e:
        print(
            f"WARNING | Could not open log file {config.LOG_PATH}: {e} — logging to stderr only",
            file=sys.stderr,
        )

    # WARNING so this always appears on stderr/journald even when the USB drive
    # is not mounted and no file handler was added.
    logger.warning(f"Pi boot — Python {sys.version.split()[0]} — log: {config.LOG_PATH}")


def configure_sync_logging() -> None:
    """Configure logging for the sync process: stderr (INFO+) only, no file.

    The sync script runs as a separate systemd oneshot; its output is captured
    by journald (`journalctl -u obd-sync`). Lowering the stderr threshold to INFO
    surfaces sync progress there without opening the collector's USB log file.
    """
    _stderr_handler.setLevel(logging.INFO)


def configure_led_logging() -> None:
    """Configure logging for the LED status process: stderr (INFO+) only, no file.

    The LED daemon runs as a separate long-lived systemd service; its output is
    captured by journald (`journalctl -u obd-led`). Like the sync process it must
    NOT attach the collector's RotatingFileHandler — sharing one file across
    processes corrupts rotation.
    """
    _stderr_handler.setLevel(logging.INFO)
