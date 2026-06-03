# Raspberry Pi Setup — Project 300K

Complete from top to bottom. Each section depends on the previous one.
Written for a fresh Pi 3B with Raspberry Pi OS Lite (Bookworm 64-bit).

---

## Hardware List

- Raspberry Pi 3B (Kano kit)
- OBDLink MX+
- OBD-II extension cable with on/off switch
- SanDisk Ultra Fit 32GB USB flash drive
- Samsung Pro Endurance 32GB microSD
- TP-Link UB500 Bluetooth USB dongle
- DS3231 RTC module with CR2032 coin cell
- Female-to-female jumper wires × 4

---

## 1. Flash microSD

1. Download **Raspberry Pi Imager** from raspberrypi.com/software
2. Insert the Samsung Pro Endurance microSD
3. In Imager: choose **Raspberry Pi OS Lite (64-bit)** — no desktop
4. Click the gear icon (advanced options) **before** flashing:
   - Hostname: `project300k`
   - Enable SSH: password authentication
   - Username: `balu` — set a password
   - Configure WiFi: add home WiFi (you'll add iPhone hotspot after boot)
   - Locale/timezone: set to yours
5. Flash, then eject

---

## 2. Wire DS3231 RTC to Pi GPIO

Connect **before first boot** — saves a reboot later.

| DS3231 pin | Pi GPIO | Pi physical pin |
|-----------|---------|----------------|
| VCC | 3.3V | Pin 1 |
| GND | GND | Pin 6 |
| SDA | GPIO 2 (SDA1) | Pin 3 |
| SCL | GPIO 3 (SCL1) | Pin 5 |

Confirm the CR2032 coin cell is seated in the DS3231.

---

## 3. First Boot

1. Insert microSD into Pi
2. Plug in TP-Link UB500 Bluetooth dongle (USB port 2, right)
3. Do **not** plug in USB flash drive yet — format it in step 6
4. Power on Pi via USB-C
5. Wait ~60 seconds, then SSH in:

```bash
ssh balu@project300k.local
```

If `.local` doesn't resolve, find the IP from your router's DHCP table and use that.

---

## 4. OS Configuration

All commands run over SSH on the Pi.

### 4a. System update

```bash
sudo apt update && sudo apt upgrade -y
```

### 4b. Install required packages

```bash
sudo apt install -y \
    i2c-tools \
    util-linux-extra \
    fake-hwclock \
    watchdog \
    python3-venv \
    git \
    bluetooth \
    bluez
```

- `util-linux-extra` — provides `hwclock` on Bookworm (not in util-linux)
- `fake-hwclock` — saves/restores time across reboots when RTC battery is dead
- `watchdog` — hardware watchdog daemon

### 4c. Edit `/boot/firmware/config.txt`

```bash
sudo nano /boot/firmware/config.txt
```

Add these lines at the bottom:

```
# Disable onboard Bluetooth (conflicts with USB BT dongle)
dtoverlay=disable-bt

# Enable DS3231 RTC on I2C-1
dtoverlay=i2c-rtc,ds3231

# Enable hardware watchdog
dtparam=watchdog=on
```

Save and exit (`Ctrl+O`, `Enter`, `Ctrl+X`).

**Order matters**: `disable-bt` must come before `i2c-rtc,ds3231`.

### 4d. Enable I2C

```bash
sudo raspi-config
```

Navigate: **Interface Options → I2C → Enable → Finish**

### 4e. Disable onboard Bluetooth service

```bash
sudo systemctl disable bluetooth
sudo systemctl disable hciuart 2>/dev/null || true
```

`hciuart` may not exist on Bookworm — the `|| true` prevents an error.

### 4f. Configure hardware watchdog daemon

```bash
sudo nano /etc/watchdog.conf
```

Find and uncomment/set these lines:

```
watchdog-device = /dev/watchdog
watchdog-timeout = 15
max-load-1 = 24
```

Enable and start the daemon:

```bash
sudo systemctl enable watchdog
sudo systemctl start watchdog
```

### 4g. Reboot to apply config.txt changes

```bash
sudo reboot
```

SSH back in after ~60 seconds.

### 4h. Verify I2C and DS3231

```bash
# Confirm I2C buses exist
ls /dev/i2c*
# Expected: /dev/i2c-1  /dev/i2c-2  (i2c-2 is internal, ignore it)

# Confirm DS3231 is detected at address 0x68
sudo i2cdetect -y 1
# Expected: shows '68' in the grid
```

### 4i. Sync system time to RTC

Do this while the Pi has internet (home WiFi) so the system time is correct.

```bash
# Sync system clock to RTC
sudo hwclock --systohc

# Verify
sudo hwclock --verbose
# Should show current time, not 2000-01-01
```

### 4j. Add iPhone hotspot WiFi

The collector syncs via iPhone hotspot when in the car.

```bash
sudo nmcli device wifi connect "BaluGadiPhone" password "YOUR_HOTSPOT_PASSWORD"
```

Confirm it saved:

```bash
nmcli connection show
# Should list "BaluGadiPhone"
```

**iPhone hotspot note**: iOS turns off hotspot when no devices are connected. Install the **Shortcuts** app on iPhone and create an automation: *When CarPlay connects → Turn Personal Hotspot On*. This ensures the hotspot is up before the Pi tries to sync.

### 4k. Install Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

This prints a URL — open it in a browser to authenticate. Once done:

```bash
tailscale ip
# Note the Pi's Tailscale IP — you'll need it for config.env
```

---

## 5. DS3231 Kernel Driver Verification

After the reboot with `dtoverlay=i2c-rtc,ds3231` active, the kernel loads the `rtc-ds1307` driver and claims the I2C address. Direct smbus2 access is blocked — use sysfs instead.

```bash
cat /sys/class/rtc/rtc0/name
# Expected: ds1307
```

If you see `ds1307`, the DS3231 is working correctly. Any other output or a missing file means check the wiring and config.txt.

---

## 6. Format and Mount USB Flash Drive

### 6a. Plug in the SanDisk Ultra Fit

```bash
lsblk
# Should show sda or sda1
```

### 6b. Format as ext4

```bash
sudo mkfs.ext4 -L obd-data /dev/sda1
```

**Warning**: this wipes the drive. If you are migrating from an old Pi and the USB drive already has data, **skip this step** — just mount the existing drive.

### 6c. Get the UUID

```bash
sudo blkid /dev/sda1
# Copy the UUID value — looks like: 1cef0a38-78c6-41cb-92e8-6fbad43bb9c8
```

### 6d. Add to fstab

```bash
sudo nano /etc/fstab
```

Add this line (replace `YOUR-UUID` with the value from blkid):

```
UUID=YOUR-UUID /mnt/usb ext4 defaults,noatime 0 2
```

### 6e. Mount and verify

```bash
sudo mkdir -p /mnt/usb
sudo systemctl daemon-reload
sudo mount -a

# Verify
ls /mnt/usb
# Expected: data  logs  lost+found  (or just lost+found on a fresh format)
```

### 6f. Create data and log directories

Skip if migrating — they already exist on the drive.

```bash
sudo mkdir -p /mnt/usb/data /mnt/usb/logs
sudo chown balu:balu /mnt/usb/data /mnt/usb/logs
```

---

## 7. Pair OBDLink MX+

The Bluetooth service must be running to pair, even though it is disabled at boot (the USB dongle takes over after pairing).

### 7a. Unblock Bluetooth and bring up the dongle

```bash
sudo rfkill unblock bluetooth
sudo systemctl start bluetooth
sudo hciconfig hci0 up
```

Verify the dongle is visible:

```bash
hciconfig
# Should show hci0 with BD Address and UP RUNNING
```

### 7b. Pair via bluetoothctl

```bash
sudo bluetoothctl
```

Inside the bluetoothctl prompt:

```
power on
agent on
default-agent
scan on
```

Wait until you see `OBDLink MX+` appear with its MAC address (`00:04:3E:8A:94:4C`), then:

```
scan off
pair 00:04:3E:8A:94:4C
trust 00:04:3E:8A:94:4C
quit
```

If prompted for a PIN, try `1234`.

### 7c. Test RFCOMM connection

```bash
sudo rfcomm connect 0 00:04:3E:8A:94:4C &
```

Wait a few seconds, then:

```bash
ls /dev/rfcomm0
# Should exist
```

Kill the background rfcomm when done testing:

```bash
sudo rfcomm release 0
kill %1
```

**Note**: `rfcomm bind` alone is not enough — you must use `rfcomm connect` to actually establish the Bluetooth connection. The `rfcomm-connect.service` handles this at boot.

---

## 8. Deploy Collector Code

### 8a. Clone the repo

```bash
cd /home/balu
git clone https://github.com/thisisbalu/project300k.git
```

### 8b. Run install.sh

```bash
cd /home/balu/project300k/pi
bash scripts/install.sh
```

This will:
- Create venv at `pi/venv/` and install Python dependencies
- Copy systemd service files to `/etc/systemd/system/`
- Enable `rfcomm-connect.service`, `obd-collector.service`, `obd-sync.timer`
- Create `/etc/obd-collector/config.env` template (if not already present)

### 8c. Fill in config.env

```bash
sudo nano /etc/obd-collector/config.env
```

Replace every `REPLACE_ME` value:

```env
API_URL=http://<HOME_SERVER_TAILSCALE_IP>:8080/sync
API_KEY=<generate below>
TAILSCALE_IP=<HOME_SERVER_TAILSCALE_IP>
OBD_PORT=/dev/rfcomm0
SYNC_BATCH_SIZE=500
DB_PATH=/mnt/usb/data/obd.db
LOG_PATH=/mnt/usb/logs/obd.log
```

Generate the API key:

```bash
openssl rand -hex 32
```

Paste the output as `API_KEY`. The same key must be configured on the home server.

### 8d. Start services

```bash
sudo systemctl start rfcomm-connect.service
sudo systemctl start obd-collector.service
sudo systemctl start obd-sync.timer
```

### 8e. Verify services started

```bash
sudo systemctl status rfcomm-connect obd-collector obd-sync.timer
```

All three should show `active`.

---

## 9. Physical Car Installation

1. Plug OBD extension cable into car's OBD-II port (under dashboard, driver side)
2. Route cable to centre console or under dash — leave the MX+ accessible
3. Mount Pi — out of direct sun, not in a sealed space, no vibration contact
4. Connect Pi USB-C power to car's built-in USB port (centre console)
5. Plug SanDisk Ultra Fit USB drive into Pi port 1 (left)
6. Plug TP-Link UB500 BT dongle into Pi port 2 (right)
7. Plug OBDLink MX+ into the OBD extension cable

---

## 10. First Real Drive Verification

### Engine off, Pi just booted

```bash
ssh balu@project300k.local
tail -f /mnt/usb/logs/obd.log
```

Expected log sequence on boot:
1. `Pi boot — Python 3.x.x — log: /mnt/usb/logs/obd.log`
2. `DS3231 RTC OK — clock is reliable`
3. `OBD connection attempt 1 on /dev/rfcomm0` — will retry until engine starts
4. After engine on: `OBD dongle verified on /dev/rfcomm0 protocol: ISO 15765-4 (CAN 11/500)`
5. `Watching PID: RPM → obd_1s.rpm every 1s` × 19 PIDs
6. `Collector started — 19 PIDs active across 3 tables`
7. `obd-collector ready`
8. `Trip started: <uuid>` — within 2 seconds of engine on
9. `DTC scan clean (trip_start)`

### While driving

Heartbeat appears every 60 seconds:

```
Heartbeat — uptime: Xm Xs | cpu: XX.X°C | mem: XXX/905MB | disk: XX.X/28.1GB | rpm: XXXX | speed: XXkm/h | pids: 19/19
```

`pids: 19/19` confirms all PIDs are firing. If you see 16/19, the Ford Mode 22 PIDs are not yet populated — expected until FORScan confirms hex addresses.

### After engine off

```
Trip ended: <uuid> — duration: Xm Xs
DTC scan clean (trip_end)
Polling paused — RPM=0 for >30s
```

### Sync verification (5 min after boot, phone hotspot required)

```bash
journalctl -u obd-sync.service -n 50
```

Expected:
```
Sync started
Network check passed
Synced obd_1s: XX rows
Synced obd_5s: XX rows
Synced obd_30s: XX rows
Sync complete
```

---

## 11. Migrating to a New Pi

The SQLite database lives on the USB flash drive, not the microSD. To migrate:

1. Take the USB flash drive from the old Pi — **your data is on it**
2. Set up the new Pi following steps 1–7 above
3. In step 6b, **skip formatting** — just mount the existing drive
4. Get the existing UUID with `sudo blkid /dev/sda1` and use it in fstab
5. Continue from step 8 — clone repo, run install.sh, fill config.env
6. The database and logs are already on the drive; the collector picks up from where it left off

The only values that change on a new Pi are:
- Pi's Tailscale IP (update your Grafana/server config if it uses the Pi's IP)
- BT pairing — same OBDLink MAC (`00:04:3E:8A:94:4C`), same pairing procedure

---

## Quick Reference

| What | Command |
|------|---------|
| Watch live logs | `tail -f /mnt/usb/logs/obd.log` |
| Collector status | `sudo systemctl status obd-collector` |
| Collector logs (systemd) | `journalctl -u obd-collector -n 50` |
| Sync logs | `journalctl -u obd-sync.service -n 50` |
| Restart collector | `sudo systemctl restart obd-collector` |
| Restart rfcomm | `sudo systemctl restart rfcomm-connect` |
| Check BT dongle | `hciconfig` |
| Check USB mount | `ls /mnt/usb` |
| DB row count | `sqlite3 /mnt/usb/data/obd.db "SELECT COUNT(*) FROM obd_1s;"` |
| Pull latest code | `cd /home/balu/project300k && git pull` |
| Update services after pull | `cd pi && bash scripts/install.sh && sudo systemctl restart obd-collector` |
