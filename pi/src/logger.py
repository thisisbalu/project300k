import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from config import config


def _build_logger() -> logging.Logger:
    log = logging.getLogger("obd-collector")
    log.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S"
    )

    # File handler — all levels, rotating 5MB x 7 files
    # Falls back to stderr if log directory not available (USB drive not mounted)
    try:
        os.makedirs(os.path.dirname(config.LOG_PATH), exist_ok=True)
        file_handler = RotatingFileHandler(
            filename=config.LOG_PATH,
            maxBytes=5 * 1024 * 1024,
            backupCount=7,
            encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        log.addHandler(file_handler)
    except OSError as e:
        print(f"WARNING | Could not open log file {config.LOG_PATH}: {e} — logging to stderr only", file=sys.stderr)

    # Console handler — WARNING and above only (journald captures this via systemd)
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(formatter)
    log.addHandler(console_handler)

    log.info(f"Pi boot — Python {sys.version.split()[0]} — log: {config.LOG_PATH}")

    return log


logger = _build_logger()
