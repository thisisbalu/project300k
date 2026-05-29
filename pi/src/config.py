"""
config.py — Configuration loader for the OBD collector.

Reads from /etc/obd-collector/config.env on startup. Any key=value pairs
in that file are loaded into the environment before validation. Values can
also be passed as plain environment variables (useful for testing).

Required values (missing any causes immediate exit):
    API_URL      — Golang sync endpoint on the home server
    API_KEY      — Bearer token for sync API authentication
    TAILSCALE_IP — Home server Tailscale IP, used for connectivity check before sync

Optional values (defaults shown):
    OBD_PORT         — /dev/rfcomm0
    SYNC_BATCH_SIZE  — 500 rows per POST
    DB_PATH          — /mnt/usb/data/obd.db
    LOG_PATH         — /mnt/usb/logs/obd.log

The module-level `config` instance is imported by all other modules.
"""

import os
import sys
from dataclasses import dataclass


CONFIG_PATH = "/etc/obd-collector/config.env"


@dataclass
class Config:
    """Validated, typed configuration for the OBD collector.

    Instantiated once at import time via _load(). All fields are read-only
    after construction — no runtime mutation.
    """

    API_URL: str
    API_KEY: str
    TAILSCALE_IP: str
    OBD_PORT: str
    SYNC_BATCH_SIZE: int
    DB_PATH: str
    LOG_PATH: str

    def __str__(self) -> str:
        """Return a log-safe string with API_KEY masked."""
        return (
            f"API_URL={self.API_URL} "
            f"TAILSCALE_IP={self.TAILSCALE_IP} "
            f"OBD_PORT={self.OBD_PORT} "
            f"SYNC_BATCH_SIZE={self.SYNC_BATCH_SIZE} "
            f"DB_PATH={self.DB_PATH} "
            f"LOG_PATH={self.LOG_PATH} "
            f"API_KEY=***"
        )


def _load() -> Config:
    """Load and validate configuration.

    Reads CONFIG_PATH line by line, populating os.environ for any
    key=value pair not already set. Falls back gracefully if the file
    is missing — allows environment-variable-only configuration for
    testing without the config file present.

    Collects all validation errors before exiting so the user sees
    every problem in one run, not one at a time.

    Returns:
        Validated Config instance.

    Exits:
        sys.exit(1) if any required value is missing or SYNC_BATCH_SIZE
        is not a positive integer.
    """
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())
    else:
        print(
            f"WARNING | Config file not found at {CONFIG_PATH}, relying on environment variables",
            file=sys.stderr,
        )

    errors = []

    required = ["API_URL", "API_KEY", "TAILSCALE_IP"]
    for key in required:
        if not os.environ.get(key):
            errors.append(f"  Missing required value: {key}")

    batch_size_raw = os.environ.get("SYNC_BATCH_SIZE", "500")
    try:
        batch_size = int(batch_size_raw)
        if batch_size <= 0:
            errors.append(
                f"  SYNC_BATCH_SIZE must be a positive integer, got: {batch_size_raw}"
            )
    except ValueError:
        errors.append(f"  SYNC_BATCH_SIZE must be an integer, got: {batch_size_raw}")
        batch_size = 500

    if errors:
        print("ERROR | Config validation failed:", file=sys.stderr)
        for e in errors:
            print(e, file=sys.stderr)
        sys.exit(1)

    return Config(
        API_URL=os.environ["API_URL"],
        API_KEY=os.environ["API_KEY"],
        TAILSCALE_IP=os.environ["TAILSCALE_IP"],
        OBD_PORT=os.environ.get("OBD_PORT", "/dev/rfcomm0"),
        SYNC_BATCH_SIZE=batch_size,
        DB_PATH=os.environ.get("DB_PATH", "/mnt/usb/data/obd.db"),
        LOG_PATH=os.environ.get("LOG_PATH", "/mnt/usb/logs/obd.log"),
    )


config = _load()
