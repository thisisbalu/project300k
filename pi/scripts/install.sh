#!/bin/bash
set -e
set -o pipefail

REPO_DIR="/home/balu/project300k"
PI_DIR="$REPO_DIR/pi"
SYSTEMD_DIR="/etc/systemd/system"
CONFIG_DIR="/etc/obd-collector"
DATA_DIR="/mnt/usb/data"
LOG_DIR="/mnt/usb/logs"

echo "==> Creating directories"
sudo mkdir -p "$CONFIG_DIR" "$DATA_DIR" "$LOG_DIR"
sudo chown balu:balu "$DATA_DIR" "$LOG_DIR"

echo "==> Creating config file (if not exists)"
if [ ! -f "$CONFIG_DIR/config.env" ]; then
    sudo tee "$CONFIG_DIR/config.env" > /dev/null <<EOF
# Project 300K — OBD Collector Configuration
# Replace every REPLACE_ME value before starting the service.
# The collector exits immediately on startup if required values are missing.

# Required — Golang sync API endpoint on home server (Tailscale IP only)
API_URL=http://REPLACE_ME_TAILSCALE_IP:8080/sync

# Required — Bearer token for API authentication
# Generate with: openssl rand -hex 32
API_KEY=REPLACE_ME_RUN_openssl_rand_-hex_32

# Required — Home server Tailscale IP (used for connectivity check before sync)
TAILSCALE_IP=REPLACE_ME_TAILSCALE_IP

# Optional — defaults shown
OBD_PORT=/dev/rfcomm0
SYNC_BATCH_SIZE=500
DB_PATH=/mnt/usb/data/obd.db
LOG_PATH=/mnt/usb/logs/obd.log
EOF
    echo "    Config created at $CONFIG_DIR/config.env"
    echo "    IMPORTANT: Replace all REPLACE_ME values before starting the service"
else
    echo "    Config already exists — skipping"
fi

echo "==> Checking VERSION file"
if [ ! -f "$PI_DIR/VERSION" ]; then
    echo "1.0.0" > "$PI_DIR/VERSION"
    echo "    VERSION file created at $PI_DIR/VERSION"
else
    echo "    VERSION already exists: $(cat "$PI_DIR/VERSION")"
fi

echo "==> Setting up Python virtualenv"
python3 -m venv "$PI_DIR/venv"
"$PI_DIR/venv/bin/pip" install --upgrade pip
"$PI_DIR/venv/bin/pip" install -r "$PI_DIR/requirements.txt"

echo "==> Installing systemd services"
sudo cp "$PI_DIR/systemd/obd-collector.service" "$SYSTEMD_DIR/"
sudo cp "$PI_DIR/systemd/obd-sync.service" "$SYSTEMD_DIR/"
sudo cp "$PI_DIR/systemd/obd-sync.timer" "$SYSTEMD_DIR/"

echo "==> Enabling services"
sudo systemctl daemon-reload
sudo systemctl enable obd-collector.service
sudo systemctl enable obd-sync.timer

echo ""
echo "Install complete."
echo "Next steps:"
echo "  1. Edit $CONFIG_DIR/config.env — replace all REPLACE_ME values"
echo "     (API_URL, API_KEY, TAILSCALE_IP are required)"
echo "  2. Generate API key: openssl rand -hex 32"
echo "  3. Pair OBDLink MX+ via bluetoothctl and bind rfcomm0"
echo "  4. sudo systemctl start obd-collector"
echo "     (TimeoutStartSec=300 — allow up to 5 min for first OBD connection)"
echo "  5. sudo systemctl start obd-sync.timer"
echo "  6. Check status: sudo systemctl status obd-collector obd-sync.timer"
echo "  7. Watch logs: tail -f /mnt/usb/logs/obd.log"
