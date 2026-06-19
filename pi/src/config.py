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
    SYNC_POLL_S      — 15 (seconds between connectivity polls / pass retries
                       in the once-per-drive sync loop)
    SYNC_GRACE_S     — 300 (after the first successful drain, hold the link up
                       this long — trailing-data window + SSH access — then do a
                       final sync and disconnect)
    DB_PATH          — /mnt/usb/data/obd.db
    LOG_PATH         — /mnt/usb/logs/obd.log

Status LED values (defaults shown) — consumed by led_status.py only:
    LED_ENABLED          — true
    LED_POLL_S           — 2.0   (fast status interval — data freshness + trip)
    LED_SLOW_POLL_S      — 30.0  (slow interval — systemd state + sync backlog)
    LED_DATA_STALE_S     — 6.0   (obd_1s older than this => OBD not flowing)
    LED_SYNC_BEHIND_DAYS — 10.0  (oldest unsynced row older than this => LED B blue)
    LED_DTC_RECENT_DAYS  — 7.0   (a DTC newer than this => LED B magenta)
    LED_CPU_WARN_C       — 75.0  (CPU hotter than this => LED A amber)
    LED_DISK_WARN_MB     — 500.0 (less free space than this => LED A amber)
    LED_A_R/G/B          — 17/27/22  (BCM pins, LED A = Pipeline)
    LED_B_R/G/B          — 5/6/13    (BCM pins, LED B = Attention)

The module-level `config` instance is imported by all other modules.
"""

import os
import sys
from dataclasses import dataclass


def _env_bool(key: str, default: bool) -> bool:
    """Read a boolean env var. Truthy: 1/true/yes/on (case-insensitive)."""
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_float(key: str, default: float) -> float:
    """Read a float env var, falling back to default on missing/invalid."""
    try:
        return float(os.environ[key])
    except (KeyError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    """Read an int env var, falling back to default on missing/invalid."""
    try:
        return int(os.environ[key])
    except (KeyError, ValueError):
        return default


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
    SYNC_POLL_S: int
    SYNC_GRACE_S: int
    DB_PATH: str
    LOG_PATH: str
    LED_ENABLED: bool
    LED_POLL_S: float
    LED_SLOW_POLL_S: float
    LED_DATA_STALE_S: float
    LED_SYNC_BEHIND_DAYS: float
    LED_DTC_RECENT_DAYS: float
    LED_CPU_WARN_C: float
    LED_DISK_WARN_MB: float
    LED_A_R: int
    LED_A_G: int
    LED_A_B: int
    LED_B_R: int
    LED_B_G: int
    LED_B_B: int

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
                    key = key.strip()
                    value = value.strip()
                    # Strip one layer of matching surrounding quotes so a value
                    # written API_KEY="abc" doesn't carry the quotes into the
                    # token. Most .env consumers do this; matching the convention
                    # avoids a silent auth failure from a copy-pasted quoted key.
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                        value = value[1:-1]
                    # setdefault only sets if not already in environment.
                    # Log which source wins so an operator editing the file
                    # while an env var is set understands why their change
                    # has no effect.
                    if key in os.environ:
                        print(
                            f"INFO  | Config: {key} sourced from environment (overrides config file)",
                            file=sys.stderr,
                        )
                    else:
                        os.environ[key] = value
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
        SYNC_POLL_S=_env_int("SYNC_POLL_S", 15),
        SYNC_GRACE_S=_env_int("SYNC_GRACE_S", 300),
        DB_PATH=os.environ.get("DB_PATH", "/mnt/usb/data/obd.db"),
        LOG_PATH=os.environ.get("LOG_PATH", "/mnt/usb/logs/obd.log"),
        LED_ENABLED=_env_bool("LED_ENABLED", True),
        LED_POLL_S=_env_float("LED_POLL_S", 2.0),
        LED_SLOW_POLL_S=_env_float("LED_SLOW_POLL_S", 30.0),
        LED_DATA_STALE_S=_env_float("LED_DATA_STALE_S", 6.0),
        LED_SYNC_BEHIND_DAYS=_env_float("LED_SYNC_BEHIND_DAYS", 10.0),
        LED_DTC_RECENT_DAYS=_env_float("LED_DTC_RECENT_DAYS", 7.0),
        LED_CPU_WARN_C=_env_float("LED_CPU_WARN_C", 75.0),
        LED_DISK_WARN_MB=_env_float("LED_DISK_WARN_MB", 500.0),
        LED_A_R=_env_int("LED_A_R", 17),
        LED_A_G=_env_int("LED_A_G", 27),
        LED_A_B=_env_int("LED_A_B", 22),
        LED_B_R=_env_int("LED_B_R", 5),
        LED_B_G=_env_int("LED_B_G", 6),
        LED_B_B=_env_int("LED_B_B", 13),
    )


config = _load()
