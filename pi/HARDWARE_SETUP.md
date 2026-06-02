# Hardware Setup Checklist

Complete these in order. Each section depends on the previous.

---

## 1. Buy Hardware ✓

- [x] OBDLink MX+ (~$120 CAD)
- [x] OBD-II extension cable with on/off switch (~$12 CAD)
- [x] SanDisk Ultra Fit 32GB USB flash drive (~$12 CAD)
- [x] Samsung Pro Endurance 32GB microSD (~$15 CAD)
- [x] TP-Link UB500 Bluetooth USB dongle (~$15 CAD)
- [x] DS3231 RTC module with CR2032 coin cell (~$8 CAD)
- [x] Female-to-female jumper wires × 4 (for DS3231 GPIO)

**Already have:** Pi 3B (Kano kit)

---

## 2. Flash microSD ✓

- [x] Download Raspberry Pi OS Lite (Bookworm 64-bit, no desktop)
- [x] Flash to Samsung Pro Endurance microSD using Raspberry Pi Imager
- [x] In Imager advanced settings before flashing:
  - [x] Set hostname: `project300k`
  - [x] Enable SSH with password auth
  - [x] Set username: `balu`, password: set
  - [x] Configured home WiFi in Imager (also add iPhone hotspot in step 4)
- [ ] Insert microSD into Pi, boot, confirm SSH works: `ssh balu@project300k.local`

---

## 3. Wire DS3231 RTC to Pi GPIO ✓

Connect DS3231 to Pi 3B GPIO header (4 wires):

| DS3231 Pin | Pi GPIO Pin | Pi Physical Pin |
|-----------|------------|----------------|
| VCC | 3.3V | Pin 1 |
| GND | GND | Pin 6 |
| SDA | GPIO 2 (SDA1) | Pin 3 |
| SCL | GPIO 3 (SCL1) | Pin 5 |

- [x] Connect DS3231 module to GPIO header with jumper wires
- [x] Confirm coin cell is inserted in DS3231

---

## 4. Pi OS Configuration (SSH into Pi) ✓

### Disable onboard Bluetooth (conflicts with USB dongle)
- [x] Add to `/boot/firmware/config.txt`: `dtoverlay=disable-bt`
- [x] Disable the BT service: `sudo systemctl disable bluetooth` (hciuart does not exist on Bookworm)

### Enable I2C (for DS3231)
- [x] Run `sudo raspi-config` → Interface Options → I2C → Enable
- [x] Reboot, then verify: `ls /dev/i2c*` — shows `/dev/i2c-1` and `/dev/i2c-2` (i2c-2 is internal, ignore it)
- [x] Confirm DS3231 is detected: `i2cdetect -y 1` — shows `68` at address 0x68

### Configure DS3231 as hardware clock
- [x] Add to `/boot/firmware/config.txt`: `dtoverlay=i2c-rtc,ds3231`
- [x] Installed `util-linux-extra` (hwclock not in util-linux on Bookworm)
- [x] Installed `fake-hwclock`
- [x] `sudo hwclock --systohc` — synced system time to RTC
- [x] Verified RTC time is correct with `sudo hwclock --verbose`

### Configure iPhone hotspot WiFi
- [x] Added via NetworkManager: `sudo nmcli device wifi connect "BaluGadiPhone" password "..."` (wpa_supplicant.conf not used on Bookworm)
- [x] Confirmed saved: `nmcli connection show` lists `BaluGadiPhone`

### Install Tailscale
- [x] `curl -fsSL https://tailscale.com/install.sh | sh`
- [x] `sudo tailscale up` — authenticated via browser
- [x] Pi Tailscale IP: `100.83.217.46`

### Enable hardware watchdog
- [x] Add to `/boot/firmware/config.txt`: `dtparam=watchdog=on`
- [x] Installed watchdog daemon: `sudo apt install -y watchdog`
- [x] Configured `/etc/watchdog.conf`: device, timeout=15, max-load-1=24
- [x] `sudo systemctl enable watchdog && sudo systemctl start watchdog` — active (running)

---

## 5. Format and Mount USB Flash Drive ✓

- [x] SanDisk Ultra Fit plugged into Pi USB port — shows as `sda1` (28.7G)
- [x] Formatted as ext4: `sudo mkfs.ext4 -L obd-data /dev/sda1`
- [x] UUID: `1cef0a38-78c6-41cb-92e8-6fbad43bb9c8`
- [x] Added to `/etc/fstab`: `UUID=1cef0a38-... /mnt/usb ext4 defaults,noatime 0 2`
- [x] `sudo systemctl daemon-reload && sudo mount -a` (daemon-reload required on Bookworm)
- [x] Created `/mnt/usb/data` and `/mnt/usb/logs`, owned by `balu:balu`
- [x] Rebooted — auto-mounts confirmed: `ls /mnt/usb/` shows `data logs lost+found`

---

## 6. Pair OBDLink MX+ ✓

- [x] USB dongle renumbered to `hci0` after onboard BT disabled (was `hci1` before)
- [x] Unblocked with `sudo rfkill unblock bluetooth` (rfkill blocked after BT service disabled)
- [x] `sudo systemctl start bluetooth` needed before bluetoothctl (service is disabled but must be running to pair)
- [x] Ran `scan on` inside bluetoothctl, then paired: OBDLink MX+ MAC: `00:04:3E:8A:94:4C`
- [x] Trusted device in bluetoothctl
- [x] `rfcomm bind` alone is not enough — must use `sudo rfcomm connect 0 00:04:3E:8A:94:4C &` to establish connection
- [x] Connection confirmed: `Connected: True`, `Protocol: ISO 15765-4 (CAN 11/500)`
- [x] udev rule created at `/etc/udev/rules.d/99-obdlink.rules` for boot-time bind (full connect behaviour to verify in step 7)

---

## 7. Deploy Collector Code

- [ ] Clone repo on Pi:
  ```bash
  git clone https://github.com/thisisbalu/project300k.git /home/pi/project300k
  ```
- [ ] Create venv and install dependencies:
  ```bash
  cd /home/pi/project300k/pi
  python3 -m venv venv
  venv/bin/pip install -r requirements.txt
  ```
- [ ] Create VERSION file:
  ```bash
  echo "1.0.0" > /home/pi/project300k/pi/VERSION
  ```
- [ ] Run install.sh:
  ```bash
  bash scripts/install.sh
  ```
- [ ] Fill in config — edit `/etc/obd-collector/config.env`:
  ```
  API_URL=http://<tailscale-ip>:8080/sync
  API_KEY=<generate with: openssl rand -hex 32>
  TAILSCALE_IP=<home-server-tailscale-ip>
  ```
  - [ ] Generate API key: `openssl rand -hex 32`
  - [ ] Confirm TAILSCALE_IP matches your home server in Tailscale admin

---

## 8. Physical Car Installation

- [ ] Plug OBD extension cable into car's OBD-II port (under dashboard, driver side)
- [ ] Route cable to a convenient spot (centre console or under dash)
- [ ] Mount Pi in car — out of direct sun, good airflow, no vibration
- [ ] Connect Pi USB power to car's built-in USB port
- [ ] Plug USB flash drive into Pi port 1 (left)
- [ ] Plug TP-Link UB500 into Pi port 2 (right)
- [ ] Plug OBDLink MX+ into OBD extension cable

---

## 9. Final Verification Before First Real Drive

- [ ] Boot Pi, SSH in, confirm all services started:
  ```bash
  sudo systemctl status obd-collector
  sudo systemctl status obd-sync.timer
  ```
- [ ] Confirm log file is on USB (not SD card):
  ```bash
  ls -la /mnt/usb/logs/obd.log
  ```
- [ ] Engine on — confirm trip started in log:
  ```bash
  tail -f /mnt/usb/logs/obd.log
  ```
- [ ] Engine off — confirm trip ended in log
- [ ] Phone hotspot connects — confirm sync fires after 5 min:
  ```bash
  journalctl -u obd-sync.service -n 30
  ```
