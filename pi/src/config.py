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


def _load() -> Config:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())

    missing = []
    required = ["API_URL", "API_KEY", "TAILSCALE_IP"]
    for key in required:
        if not os.environ.get(key):
            missing.append(key)

    if missing:
        print(f"ERROR | Missing required config: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    return Config(
        API_URL=os.environ["API_URL"],
        API_KEY=os.environ["API_KEY"],
        TAILSCALE_IP=os.environ["TAILSCALE_IP"],
        OBD_PORT=os.environ.get("OBD_PORT", "/dev/rfcomm0"),
        SYNC_BATCH_SIZE=int(os.environ.get("SYNC_BATCH_SIZE", "500")),
        DB_PATH=os.environ.get("DB_PATH", "/mnt/usb/data/obd.db"),
        LOG_PATH=os.environ.get("LOG_PATH", "/mnt/usb/logs/obd.log"),
    )


config = _load()
