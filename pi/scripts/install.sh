#!/bin/bash
set -e

REPO_DIR="/home/pi/project300k"
PI_DIR="$REPO_DIR/pi"
SYSTEMD_DIR="/etc/systemd/system"
CONFIG_DIR="/etc/obd-collector"
DATA_DIR="/mnt/usb/data"
LOG_DIR="/mnt/usb/logs"

echo "==> Creating directories"
sudo mkdir -p "$CONFIG_DIR" "$DATA_DIR" "$LOG_DIR"
sudo chown pi:pi "$DATA_DIR" "$LOG_DIR"

echo "==> Creating config file (if not exists)"
if [ ! -f "$CONFIG_DIR/config.env" ]; then
    sudo tee "$CONFIG_DIR/config.env" > /dev/null <<EOF
API_URL=http://<tailscale-ip>:8080/sync
API_KEY=<generated-with-openssl-rand-hex-32>
TAILSCALE_IP=<tailscale-ip>
OBD_PORT=/dev/rfcomm0
SYNC_BATCH_SIZE=500
DB_PATH=/mnt/usb/data/obd.db
LOG_PATH=/mnt/usb/logs/obd.log
EOF
    echo "    Config created at $CONFIG_DIR/config.env — fill in API_URL, API_KEY, TAILSCALE_IP"
else
    echo "    Config already exists — skipping"
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
echo "  1. Fill in $CONFIG_DIR/config.env with API_URL, API_KEY, TAILSCALE_IP"
echo "  2. Pair OBDLink MX+ and bind rfcomm0"
echo "  3. sudo systemctl start obd-collector"
echo "  4. sudo systemctl start obd-sync.timer"
