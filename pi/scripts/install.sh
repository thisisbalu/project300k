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
# Seconds between connectivity polls / pass retries in the once-per-drive sync loop
SYNC_POLL_S=15
DB_PATH=/mnt/usb/data/obd.db
LOG_PATH=/mnt/usb/logs/obd.log

# Hotspot NetworkManager connection name (the iPhone SSID as NM lists it under
# 'nmcli connection show'). Used only by install.sh to keep autoconnect ON so the
# Pi connects proactively. Autoconnect is the NM default anyway; set this to have
# install.sh enforce it.
HOTSPOT_CONN=REPLACE_ME_HOTSPOT_SSID

# Status LEDs (optional — defaults shown). See pi/CLAUDE.md for the behaviour spec.
LED_ENABLED=true
LED_SYNC_BEHIND_DAYS=10
LED_DTC_RECENT_DAYS=7
LED_CPU_WARN_C=75
# GPIO (BCM) pins — LED A = Pipeline, LED B = Attention
LED_A_R=17
LED_A_G=27
LED_A_B=22
LED_B_R=5
LED_B_G=6
LED_B_B=13
EOF
    echo "    Config created at $CONFIG_DIR/config.env"
    echo "    IMPORTANT: Replace all REPLACE_ME values before starting the service"
else
    echo "    Config already exists — skipping"
fi

# config.env holds the API bearer token — lock it to owner-only so other local
# users/processes cannot read the secret. `sudo tee` creates it world-readable
# (root umask 022 → 0644); enforce 0600 and balu ownership every run (idempotent).
sudo chown balu:balu "$CONFIG_DIR/config.env"
sudo chmod 600 "$CONFIG_DIR/config.env"

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
"$PI_DIR/venv/bin/pip" install datasette
# lgpio is the gpiozero pin-factory backend for the status LEDs. Pi-only —
# it is a C extension that does not build on the Mac dev machine, so it is
# installed here rather than in requirements.txt (which must stay Mac-safe).
# Newer Raspberry Pi OS (Python 3.13 / Trixie) has no prebuilt lgpio wheel, so
# pip compiles it from source: that needs swig + python headers + the lgpio C
# library (liblgpio-dev) or the build fails (missing swig / -llgpio).
sudo apt-get install -y swig python3-dev liblgpio-dev
"$PI_DIR/venv/bin/pip" install lgpio

echo "==> Installing jarvis"
chmod +x "$PI_DIR/scripts/jarvis"
sudo ln -sf "$PI_DIR/scripts/jarvis" /usr/local/bin/jarvis

echo "==> Installing systemd services"
sudo cp "$PI_DIR/systemd/rfcomm-connect.service" "$SYSTEMD_DIR/"
sudo cp "$PI_DIR/systemd/obd-collector.service" "$SYSTEMD_DIR/"
sudo cp "$PI_DIR/systemd/obd-sync.service" "$SYSTEMD_DIR/"
sudo cp "$PI_DIR/systemd/obd-datasette.service" "$SYSTEMD_DIR/"
sudo cp "$PI_DIR/systemd/obd-led.service" "$SYSTEMD_DIR/"

# Remove the old recurring sync timer if a previous install left it behind —
# sync now runs once per drive via obd-sync.service (enabled below).
sudo systemctl disable obd-sync.timer 2>/dev/null || true
sudo rm -f "$SYSTEMD_DIR/obd-sync.timer"

echo "==> Enabling services"
sudo systemctl daemon-reload
sudo systemctl enable rfcomm-connect.service
sudo systemctl enable obd-collector.service
sudo systemctl enable obd-sync.service
sudo systemctl enable obd-led.service

# Keep the hotspot autoconnect ON so the Pi connects proactively. Autoconnect is
# the NM default; this enforces it if HOTSPOT_CONN names a real profile.
HOTSPOT_CONN=$(grep -E '^HOTSPOT_CONN=' "$CONFIG_DIR/config.env" 2>/dev/null | cut -d= -f2-)
if [ -n "$HOTSPOT_CONN" ] && [ "$HOTSPOT_CONN" != "REPLACE_ME_HOTSPOT_SSID" ] \
   && nmcli -t -f NAME connection show 2>/dev/null | grep -qxF "$HOTSPOT_CONN"; then
    sudo nmcli connection modify "$HOTSPOT_CONN" connection.autoconnect yes
    echo "    autoconnect=yes ensured on hotspot profile '$HOTSPOT_CONN'"
else
    echo "    Hotspot autoconnect: set HOTSPOT_CONN in config.env to enforce (NM default is already on)"
fi
# obd-datasette is deliberately NOT enabled — it is on-demand only (started via
# `jarvis datasette start`). It serves the full DB read-only and binds localhost,
# so leaving it off at boot avoids running an unneeded listener 24/7.

echo ""
echo "Install complete."
echo "Next steps:"
echo "  1. Edit $CONFIG_DIR/config.env — replace all REPLACE_ME values"
echo "     (API_URL, API_KEY, TAILSCALE_IP are required)"
echo "  2. Generate API key: openssl rand -hex 32"
echo "  3. Pair OBDLink MX+ via bluetoothctl and bind rfcomm0"
echo "  4. sudo systemctl start obd-collector"
echo "     (TimeoutStartSec=300 — allow up to 5 min for first OBD connection)"
echo "  5. sudo systemctl start obd-sync.service   (syncs once per drive; also auto-runs at boot)"
echo "  6. sudo systemctl start obd-led      (verify wiring first: jarvis led test)"
echo "  7. Check status: sudo systemctl status obd-collector obd-sync.service obd-led"
echo "  8. Watch logs: tail -f /mnt/usb/logs/obd.log"
