"""
led_status.py — Status LEDs for the OBD collector (separate systemd service).

Drives two KY-016 common-cathode RGB LEDs wired directly to the Pi GPIO header
(onboard 1kΩ resistors, so active-high with no external parts). Runs as its own
long-lived process — it never touches the collector. Like the sync script it
only reads state: systemd unit status, the kernel sysfs interfaces, and
read-only SELECTs against the SQLite database the collector writes (WAL allows
concurrent readers). This keeps the LEDs fully decoupled from the data pipeline.

LED A — Pipeline (is data being recorded?)
    off    no power / collector process down
    blue   parked / connecting — up but OBD not connected, no fault
    green  OBD connected, data flowing
    red    FAULT — BT dongle missing / USB unmounted / OBD stalled mid-trip
    amber  Pi warning, still capturing — hot CPU / low disk / RTC bad
    priority: off > red > amber > green > blue

LED B — Attention (does it need me?) — dark when all is well
    off      synced, no trip, no faults
    green*   trip active, logging                 (* = slow blink)
    blue     sync behind — oldest unsynced row older than LED_SYNC_BEHIND_DAYS
    magenta  DTC fault present (recent)
    priority: magenta > blue > green > off

evaluate_state() is a pure function so the whole behaviour spec is unit-testable
without hardware. The gpiozero import lives inside LedDriver so importing this
module (e.g. under pytest on a non-Pi machine) never needs a GPIO backend.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import psutil

import health
from config import config
from logger import configure_led_logging, logger

# RGB channel values for gpiozero (0.0–1.0 per channel; PWM duty cycle).
OFF = (0.0, 0.0, 0.0)
RED = (1.0, 0.0, 0.0)
GREEN = (0.0, 1.0, 0.0)
BLUE = (0.0, 0.0, 1.0)
# 1kΩ resistors drop green/blue current far more than red, so an even R+G mix
# reads orange-ish — bias toward red for a recognisable amber.
AMBER = (1.0, 0.35, 0.0)
MAGENTA = (1.0, 0.0, 1.0)


@dataclass(frozen=True)
class Display:
    """A target LED appearance: a colour and whether it slow-blinks."""

    color: tuple
    blink: bool = False


# LED A — Pipeline
A_OFF = Display(OFF)
A_BLUE = Display(BLUE)
A_GREEN = Display(GREEN)
A_RED = Display(RED)
A_AMBER = Display(AMBER)

# LED B — Attention
B_OFF = Display(OFF)
B_GREEN = Display(GREEN, blink=True)
B_BLUE = Display(BLUE)
B_MAGENTA = Display(MAGENTA)

# OBD reading tables that count toward the sync backlog (trips/dtc/health excluded).
_BACKLOG_TABLES = (
    "obd_1s", "obd_5s", "obd_30s",
    "ford_obd_5s", "ford_obd_10s", "ford_obd_20s",
)


@dataclass
class Signals:
    """Boolean snapshot of everything the two LEDs are derived from."""

    collector_active: bool
    usb_mounted: bool
    bt_adapter_present: bool
    data_fresh: bool
    trip_active: bool
    pi_warning: bool
    sync_behind: bool
    dtc_present: bool


def evaluate_state(s: Signals) -> tuple[Display, Display]:
    """Map a Signals snapshot to (LED A display, LED B display).

    Pure function — no I/O — so every state and priority collision is testable
    without hardware. See the module docstring for the colour legend.
    """
    if not s.collector_active:
        led_a = A_OFF
    elif (not s.usb_mounted
          or not s.bt_adapter_present
          or (s.trip_active and not s.data_fresh)):
        led_a = A_RED
    elif s.pi_warning:
        led_a = A_AMBER
    elif s.data_fresh:
        led_a = A_GREEN
    else:
        led_a = A_BLUE

    if s.dtc_present:
        led_b = B_MAGENTA
    elif s.sync_behind:
        led_b = B_BLUE
    elif s.trip_active:
        led_b = B_GREEN
    else:
        led_b = B_OFF

    return led_a, led_b


def _parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO8601 timestamp from the DB into an aware UTC datetime.

    Stored timestamps carry a +00:00 offset; naive values are assumed UTC so a
    comparison against datetime.now(timezone.utc) never raises.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _collector_active() -> bool:
    """True if obd-collector.service is up (running or starting up).

    The collector is Type=notify, so systemd only reports "active" after the
    OBD link connects and the process sends READY=1. The whole connect/retry
    phase reports "activating" — the process is up and trying, which is the
    spec's blue ("up but OBD not connected") state, not off. Counting both
    means LED A shows blue while connecting instead of going dark; only a
    genuinely stopped/failed unit (inactive/failed) maps to off.
    """
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "obd-collector.service"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() in ("active", "activating")
    except (OSError, subprocess.SubprocessError):
        return False


def _open_db() -> sqlite3.Connection | None:
    """Open a short-lived read-only handle to the collector's database.

    A fresh connection per poll (closed immediately after) avoids holding a
    read transaction open between polls, which would pin the WAL and block the
    collector's checkpoint. query_only=ON hard-guards against any accidental
    write. Caller must confirm the file exists first — sqlite3.connect would
    otherwise create an empty DB on the SD card if the USB drive is unmounted.
    """
    try:
        conn = sqlite3.connect(config.DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn
    except sqlite3.Error as e:
        logger.debug(f"LED: cannot open DB: {e}")
        return None


def _data_fresh(conn: sqlite3.Connection, now: datetime) -> bool:
    """True if the newest obd_1s row is within LED_DATA_STALE_S of now."""
    try:
        row = conn.execute("SELECT MAX(timestamp) FROM obd_1s").fetchone()
    except sqlite3.Error:
        return False
    ts = _parse_ts(row[0]) if row else None
    return ts is not None and (now - ts).total_seconds() <= config.LED_DATA_STALE_S


def _has_open_trip(conn: sqlite3.Connection) -> bool:
    """True if a trip is currently active (a trips row with end_time NULL)."""
    try:
        row = conn.execute(
            "SELECT 1 FROM trips WHERE end_time IS NULL LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _has_recent_dtc(conn: sqlite3.Connection, now: datetime) -> bool:
    """True if a DTC was recorded within LED_DTC_RECENT_DAYS."""
    try:
        row = conn.execute("SELECT MAX(timestamp) FROM dtc_events").fetchone()
    except sqlite3.Error:
        return False
    ts = _parse_ts(row[0]) if row else None
    return ts is not None and (now - ts).total_seconds() <= config.LED_DTC_RECENT_DAYS * 86400


def _is_sync_behind(conn: sqlite3.Connection, now: datetime) -> bool:
    """True if any OBD table's oldest unsynced row is older than the threshold.

    Normal operation keeps the oldest unsynced row minutes old (synced every
    5 min); a value past LED_SYNC_BEHIND_DAYS means sync has been failing.
    """
    threshold_s = config.LED_SYNC_BEHIND_DAYS * 86400
    for table in _BACKLOG_TABLES:
        try:
            row = conn.execute(
                f"SELECT MIN(timestamp) FROM {table} WHERE synced=0"
            ).fetchone()
        except sqlite3.Error:
            continue
        ts = _parse_ts(row[0]) if row else None
        if ts is not None and (now - ts).total_seconds() > threshold_s:
            return True
    return False


def _pi_warning(usb_mounted: bool) -> bool:
    """True if the Pi is degraded but still capturing — drives LED A amber."""
    temp = health._read_cpu_temp()
    if temp is not None and temp >= config.LED_CPU_WARN_C:
        return True
    if not health.read_rtc_ok():
        return True
    if usb_mounted:
        try:
            disk = psutil.disk_usage(health.USB_MOUNT_PATH)
            if disk.free / 1024 / 1024 < config.LED_DISK_WARN_MB:
                return True
        except OSError:
            pass
    return False


def read_signals() -> Signals:
    """Gather the current status snapshot from systemd, sysfs, and the DB."""
    usb_mounted = bool(health._check_usb_mounted())
    now = datetime.now(timezone.utc)

    data_fresh = trip_active = sync_behind = dtc_present = False
    # Only open the DB when the drive is mounted AND the file exists — otherwise
    # sqlite3.connect would create a stray empty DB on the SD card root.
    if usb_mounted and os.path.exists(config.DB_PATH):
        conn = _open_db()
        if conn is not None:
            try:
                data_fresh = _data_fresh(conn, now)
                trip_active = _has_open_trip(conn)
                dtc_present = _has_recent_dtc(conn, now)
                sync_behind = _is_sync_behind(conn, now)
            finally:
                conn.close()

    return Signals(
        collector_active=_collector_active(),
        usb_mounted=usb_mounted,
        bt_adapter_present=bool(health._check_bt_adapter()),
        data_fresh=data_fresh,
        trip_active=trip_active,
        pi_warning=_pi_warning(usb_mounted),
        sync_behind=sync_behind,
        dtc_present=dtc_present,
    )


class LedDriver:
    """Wraps two gpiozero RGBLEDs and only re-issues a colour when it changes.

    gpiozero is imported here, not at module scope, so the rest of the module
    imports cleanly on a non-Pi machine for testing. Re-applying the same
    Display every poll would restart the blink thread and stutter the blink, so
    apply() tracks the currently shown Display per LED and skips no-op writes.
    """

    def __init__(self) -> None:
        from gpiozero import RGBLED

        self._a = RGBLED(
            red=config.LED_A_R, green=config.LED_A_G, blue=config.LED_A_B,
            active_high=True, pwm=True,
        )
        self._b = RGBLED(
            red=config.LED_B_R, green=config.LED_B_G, blue=config.LED_B_B,
            active_high=True, pwm=True,
        )
        self._cur_a: Display | None = None
        self._cur_b: Display | None = None

    def apply(self, led_a: Display, led_b: Display) -> None:
        """Update each LED only if its target Display changed."""
        if led_a != self._cur_a:
            self._set(self._a, led_a)
            self._cur_a = led_a
        if led_b != self._cur_b:
            self._set(self._b, led_b)
            self._cur_b = led_b

    @staticmethod
    def _set(led, display: Display) -> None:
        if display.blink:
            led.blink(
                on_time=0.6, off_time=0.6,
                on_color=display.color, off_color=OFF,
                background=True,
            )
        else:
            led.color = display.color

    def off(self) -> None:
        """Turn both LEDs off and release the GPIO pins."""
        try:
            self._a.off()
            self._b.off()
        finally:
            self._a.close()
            self._b.close()


class LedStatus:
    """Polls read_signals() every LED_POLL_S and drives the LEDs accordingly."""

    def __init__(self, driver: LedDriver) -> None:
        self._driver = driver
        self._stop = threading.Event()

    def stop(self, *_args) -> None:
        """Signal the run loop to exit — used as the SIGTERM handler."""
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                led_a, led_b = evaluate_state(read_signals())
                self._driver.apply(led_a, led_b)
            except Exception as e:  # never let a transient read error kill the daemon
                logger.error(f"LED status loop error: {e}")
            self._stop.wait(config.LED_POLL_S)


def _test_cycle(driver: LedDriver) -> None:
    """Walk both LEDs through every state so wiring can be verified by eye."""
    sequence = [
        ("A red / B magenta", A_RED, B_MAGENTA),
        ("A amber / B blue", A_AMBER, B_BLUE),
        ("A green / B green(blink)", A_GREEN, B_GREEN),
        ("A blue / B off", A_BLUE, B_OFF),
    ]
    for label, led_a, led_b in sequence:
        logger.info(f"LED test: {label}")
        driver.apply(led_a, led_b)
        time.sleep(2)


def run_test() -> None:  # pragma: no cover
    """Entry point for `jarvis led test` — cycle colours then release pins."""
    configure_led_logging()
    driver = LedDriver()
    try:
        _test_cycle(driver)
    finally:
        driver.off()


def main() -> None:  # pragma: no cover
    """Service entry point — run the status loop until SIGTERM."""
    configure_led_logging()
    logger.info("LED status starting")

    if not config.LED_ENABLED:
        logger.info("LED status disabled (LED_ENABLED=false) — exiting")
        return

    try:
        driver = LedDriver()
    except Exception as e:
        logger.error(f"Failed to initialise LED GPIO: {e} — exiting")
        return

    app = LedStatus(driver)
    signal.signal(signal.SIGTERM, app.stop)
    try:
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        driver.off()
        logger.info("LED status stopped")


if __name__ == "__main__":  # pragma: no cover
    import sys

    if "--test" in sys.argv:
        run_test()
    else:
        main()
