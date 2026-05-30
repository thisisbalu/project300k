# Hardware Setup Checklist

Complete these in order. Each section depends on the previous.

---

## 1. Buy Hardware

- [ ] OBDLink MX+ (~$120 CAD)
- [ ] OBD-II extension cable with on/off switch (~$12 CAD)
- [ ] SanDisk Ultra Fit 32GB USB flash drive (~$12 CAD)
- [ ] Samsung Pro Endurance 32GB microSD (~$15 CAD)
- [ ] TP-Link UB500 Bluetooth USB dongle (~$15 CAD)
- [ ] DS3231 RTC module with CR2032 coin cell (~$8 CAD)
- [ ] Female-to-female jumper wires × 4 (for DS3231 GPIO)

**Already have:** Pi 3B (Kano kit)

---

## 2. Flash microSD

- [ ] Download Raspberry Pi OS Lite (Bookworm 64-bit, no desktop)
- [ ] Flash to Samsung Pro Endurance microSD using Raspberry Pi Imager
- [ ] In Imager advanced settings before flashing:
  - [ ] Set hostname: `bronco-pi`
  - [ ] Enable SSH with password auth
  - [ ] Set username: `pi`, password: choose a strong one
  - [ ] Do NOT configure WiFi here — done in wpa_supplicant later
- [ ] Insert microSD into Pi, boot, confirm SSH works: `ssh pi@bronco-pi.local`

---

## 3. Wire DS3231 RTC to Pi GPIO

Connect DS3231 to Pi 3B GPIO header (4 wires):

| DS3231 Pin | Pi GPIO Pin | Pi Physical Pin |
|-----------|------------|----------------|
| VCC | 3.3V | Pin 1 |
| GND | GND | Pin 6 |
| SDA | GPIO 2 (SDA1) | Pin 3 |
| SCL | GPIO 3 (SCL1) | Pin 5 |

- [ ] Connect DS3231 module to GPIO header with jumper wires
- [ ] Confirm coin cell is inserted in DS3231

---

## 4. Pi OS Configuration (SSH into Pi)

### Disable onboard Bluetooth (conflicts with USB dongle)
- [ ] Add to `/boot/firmware/config.txt`:
  ```
  dtoverlay=disable-bt
  ```
- [ ] Disable the BT service:
  ```bash
  sudo systemctl disable hciuart
  sudo systemctl disable bluetooth
  ```

### Enable I2C (for DS3231)
- [ ] Run `sudo raspi-config` → Interface Options → I2C → Enable
- [ ] Reboot, then verify: `ls /dev/i2c*` should show `/dev/i2c-1`
- [ ] Confirm DS3231 is detected:
  ```bash
  sudo apt install -y i2c-tools
  i2cdetect -y 1   # should show 0x68
  ```

### Configure DS3231 as hardware clock
- [ ] Add to `/boot/firmware/config.txt`:
  ```
  dtoverlay=i2c-rtc,ds3231
  ```
- [ ] Reboot, then:
  ```bash
  sudo apt install -y fake-hwclock
  sudo hwclock --systohc         # write current time to RTC
  sudo hwclock --verbose         # confirm RTC time is correct
  ```
- [ ] Verify time survives reboot without internet: disconnect ethernet, reboot, check `date`

### Configure iPhone hotspot WiFi
- [ ] Add to `/etc/wpa_supplicant/wpa_supplicant.conf`:
  ```
  network={
      ssid="Your iPhone Name"
      psk="your-hotspot-password"
      key_mgmt=WPA-PSK
  }
  ```
- [ ] `sudo wpa_cli reconfigure` then `ip addr show wlan0` — confirm IP assigned

### Install Tailscale
- [ ] `curl -fsSL https://tailscale.com/install.sh | sh`
- [ ] `sudo tailscale up` — authenticate via browser link
- [ ] Confirm Pi appears in Tailscale admin console with a 100.x.x.x IP

### Enable hardware watchdog
- [ ] Add to `/boot/firmware/config.txt`:
  ```
  dtparam=watchdog=on
  ```
- [ ] Install watchdog daemon:
  ```bash
  sudo apt install -y watchdog
  ```
- [ ] Edit `/etc/watchdog.conf`:
  ```
  watchdog-device = /dev/watchdog
  watchdog-timeout = 15
  max-load-1 = 24
  ```
- [ ] `sudo systemctl enable watchdog && sudo systemctl start watchdog`

---

## 5. Format and Mount USB Flash Drive

- [ ] Plug SanDisk Ultra Fit into Pi USB port 1
- [ ] Find the device: `lsblk` — note the device name (e.g. `/dev/sda`)
- [ ] Format as ext4:
  ```bash
  sudo mkfs.ext4 -L obd-data /dev/sda1
  ```
- [ ] Get UUID: `sudo blkid /dev/sda1` — copy the UUID
- [ ] Add to `/etc/fstab`:
  ```
  UUID=<your-uuid>  /mnt/usb  ext4  defaults,noatime  0  2
  ```
- [ ] `sudo mkdir -p /mnt/usb && sudo mount -a`
- [ ] Confirm mounted: `df -h /mnt/usb`
- [ ] Create directories:
  ```bash
  sudo mkdir -p /mnt/usb/data /mnt/usb/logs
  sudo chown -R pi:pi /mnt/usb/data /mnt/usb/logs
  ```
- [ ] Reboot and confirm USB auto-mounts: `ls /mnt/usb/`

---

## 6. Pair OBDLink MX+

Plug TP-Link UB500 into Pi USB port 2. Engine must be on (or ignition in accessory mode) for OBDLink to be discoverable.

- [ ] Confirm USB dongle is detected:
  ```bash
  hciconfig       # should show hci0
  hcitool scan    # scan for devices — OBDLink should appear
  ```
- [ ] Pair via bluetoothctl:
  ```bash
  bluetoothctl
  > power on
  > agent on
  > scan on
  # wait for OBDLink MX+ to appear, note its MAC (e.g. AA:BB:CC:DD:EE:FF)
  > pair AA:BB:CC:DD:EE:FF
  > trust AA:BB:CC:DD:EE:FF
  > quit
  ```
- [ ] Bind to rfcomm0:
  ```bash
  sudo rfcomm bind 0 AA:BB:CC:DD:EE:FF
  ls -la /dev/rfcomm0   # confirm exists
  ```
- [ ] Set up rfcomm bind on boot — create `/etc/udev/rules.d/99-obdlink.rules`:
  ```
  ACTION=="add", KERNEL=="hci0", RUN+="/usr/bin/rfcomm bind 0 AA:BB:CC:DD:EE:FF"
  ```
- [ ] Test connection manually:
  ```bash
  /home/pi/project300k/pi/venv/bin/python3 -c "
  import obd
  conn = obd.OBD('/dev/rfcomm0', fast=False, timeout=30)
  print('Connected:', conn.is_connected())
  print('Protocol:', conn.protocol_name())
  conn.close()
  "
  ```

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
