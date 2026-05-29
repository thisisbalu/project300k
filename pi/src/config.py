import os
import sys
from dataclasses import dataclass


CONFIG_PATH = "/etc/obd-collector/config.env"


@dataclass
class Config:
    API_URL: str
    API_KEY: str
    TAILSCALE_IP: str
    OBD_PORT: str
    SYNC_BATCH_SIZE: int
    DB_PATH: str
    LOG_PATH: str

    def __str__(self) -> str:
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
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())
    else:
        print(f"WARNING | Config file not found at {CONFIG_PATH}, relying on environment variables", file=sys.stderr)

    errors = []

    required = ["API_URL", "API_KEY", "TAILSCALE_IP"]
    for key in required:
        if not os.environ.get(key):
            errors.append(f"  Missing required value: {key}")

    batch_size_raw = os.environ.get("SYNC_BATCH_SIZE", "500")
    try:
        batch_size = int(batch_size_raw)
        if batch_size <= 0:
            errors.append(f"  SYNC_BATCH_SIZE must be a positive integer, got: {batch_size_raw}")
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
