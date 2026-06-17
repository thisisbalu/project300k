"""Tests for health.py — Pi metric collection, restart counter, filesystem checks."""

import os
from unittest.mock import MagicMock, mock_open, patch

import pytest


# ---------------------------------------------------------------------------
# increment_restart_count()
# ---------------------------------------------------------------------------

class TestRtcOkFile:
    def test_write_then_read_roundtrip(self, tmp_path):
        from health import write_rtc_ok, read_rtc_ok
        path = str(tmp_path / "rtc_ok")
        with patch("health.RTC_OK_PATH", path):
            write_rtc_ok(0)
            assert read_rtc_ok() == 0

    def test_read_returns_1_when_file_missing(self, tmp_path):
        from health import read_rtc_ok
        with patch("health.RTC_OK_PATH", str(tmp_path / "nonexistent")):
            assert read_rtc_ok() == 1

    def test_read_returns_1_on_corrupt_file(self, tmp_path):
        from health import read_rtc_ok
        path = str(tmp_path / "rtc_ok")
        open(path, "w").write("bad")
        with patch("health.RTC_OK_PATH", path):
            assert read_rtc_ok() == 1

    def test_write_failure_logs_warning(self, tmp_path, caplog):
        import logging
        from health import write_rtc_ok
        with patch("health.RTC_OK_PATH", "/nonexistent/dir/file"), \
             caplog.at_level(logging.WARNING, logger="obd-collector"):
            write_rtc_ok(1)
        assert "Could not write rtc_ok" in caplog.text

    def test_write_1_and_0_roundtrip(self, tmp_path):
        from health import write_rtc_ok, read_rtc_ok
        path = str(tmp_path / "rtc_ok")
        with patch("health.RTC_OK_PATH", path):
            write_rtc_ok(1)
            assert read_rtc_ok() == 1
            write_rtc_ok(0)
            assert read_rtc_ok() == 0


class TestRestartCountFile:
    def test_read_returns_0_when_file_missing(self, tmp_path):
        from health import read_restart_count
        with patch("health.RESTART_COUNT_PATH", str(tmp_path / "nonexistent")):
            assert read_restart_count() == 0

    def test_write_then_read_roundtrip(self, tmp_path):
        from health import increment_restart_count, read_restart_count
        path = str(tmp_path / "restart_count")
        open(path, "w").write("4")
        with patch("health.RESTART_COUNT_PATH", path):
            increment_restart_count()   # bumps to 5
            assert read_restart_count() == 5

    def test_read_does_not_increment(self, tmp_path):
        from health import read_restart_count
        path = str(tmp_path / "restart_count")
        open(path, "w").write("3")
        with patch("health.RESTART_COUNT_PATH", path):
            assert read_restart_count() == 3
            assert read_restart_count() == 3   # second call still returns 3

    def test_corrupt_file_returns_0(self, tmp_path):
        from health import read_restart_count
        path = str(tmp_path / "restart_count")
        open(path, "w").write("notanumber")
        with patch("health.RESTART_COUNT_PATH", path):
            assert read_restart_count() == 0


class TestReconnectCountFile:
    def test_read_returns_0_when_file_missing(self, tmp_path):
        from health import read_reconnect_count
        with patch("health.RECONNECT_COUNT_PATH", str(tmp_path / "nonexistent")):
            assert read_reconnect_count() == 0

    def test_write_then_read_roundtrip(self, tmp_path):
        from health import write_reconnect_count, read_reconnect_count
        path = str(tmp_path / "reconnect_count")
        with patch("health.RECONNECT_COUNT_PATH", path):
            write_reconnect_count(7)
            assert read_reconnect_count() == 7

    def test_write_failure_logs_warning(self, tmp_path, caplog):
        import logging
        from health import write_reconnect_count
        with patch("health.RECONNECT_COUNT_PATH", "/nonexistent/dir/file"), \
             caplog.at_level(logging.WARNING, logger="obd-collector"):
            write_reconnect_count(3)
        assert "Could not write reconnect count" in caplog.text


class TestIncrementRestartCount:
    def test_first_boot_creates_file_returns_1(self, tmp_path):
        from health import increment_restart_count, RESTART_COUNT_PATH
        path = str(tmp_path / "restart_count")
        with patch("health.RESTART_COUNT_PATH", path):
            count = increment_restart_count()
        assert count == 1
        assert open(path).read() == "1"

    def test_subsequent_boots_increment(self, tmp_path):
        from health import increment_restart_count
        path = str(tmp_path / "restart_count")
        open(path, "w").write("4")
        with patch("health.RESTART_COUNT_PATH", path):
            count = increment_restart_count()
        assert count == 5
        assert open(path).read() == "5"

    def test_corrupt_file_resets_to_1(self, tmp_path, caplog):
        import logging
        from health import increment_restart_count
        path = str(tmp_path / "restart_count")
        open(path, "w").write("notanumber")
        with patch("health.RESTART_COUNT_PATH", path), \
             caplog.at_level(logging.WARNING, logger="obd-collector"):
            count = increment_restart_count()
        assert count == 1
        assert "Could not read" in caplog.text

    def test_write_calls_fsync(self, tmp_path):
        """File must be fsync'd so the count survives an abrupt power cut."""
        from health import increment_restart_count
        path = str(tmp_path / "restart_count")
        fsynced = []
        original_fsync = os.fsync

        def capture_fsync(fd):
            fsynced.append(fd)
            original_fsync(fd)

        with patch("health.RESTART_COUNT_PATH", path), \
             patch("os.fsync", side_effect=capture_fsync):
            increment_restart_count()
        assert fsynced

    def test_write_failure_logs_warning(self, tmp_path, caplog):
        import logging
        from health import increment_restart_count
        path = str(tmp_path / "restart_count")
        with patch("health.RESTART_COUNT_PATH", path), \
             patch("builtins.open", side_effect=[
                 FileNotFoundError,           # exists check via open (pre-read)
                 OSError("disk full"),         # write
             ]), \
             patch("os.path.exists", return_value=False), \
             caplog.at_level(logging.WARNING, logger="obd-collector"):
            increment_restart_count()
        assert "Could not write restart count" in caplog.text

    def test_missing_file_treated_as_zero(self, tmp_path):
        from health import increment_restart_count
        path = str(tmp_path / "nonexistent")
        with patch("health.RESTART_COUNT_PATH", path):
            count = increment_restart_count()
        assert count == 1


# ---------------------------------------------------------------------------
# _read_cpu_temp()
# ---------------------------------------------------------------------------

class TestReadCpuTemp:
    def test_reads_and_divides_by_1000(self, tmp_path):
        from health import _read_cpu_temp
        temp_file = tmp_path / "temp"
        temp_file.write_text("52000\n")
        with patch("health.CPU_TEMP_PATH", str(temp_file)):
            assert _read_cpu_temp() == pytest.approx(52.0)

    def test_returns_none_when_file_missing(self):
        from health import _read_cpu_temp
        with patch("health.CPU_TEMP_PATH", "/nonexistent/temp"):
            assert _read_cpu_temp() is None


# ---------------------------------------------------------------------------
# _read_last_error()
# ---------------------------------------------------------------------------

class TestReadLastError:
    def test_returns_last_error_line(self, tmp_path):
        from health import _read_last_error
        log_file = tmp_path / "obd.log"
        log_file.write_text(
            "2026-01-01 | INFO     | Trip started\n"
            "2026-01-01 | ERROR    | SQLite write failed\n"
            "2026-01-01 | INFO     | Watchdog ping\n"
        )
        from config import config
        with patch.object(config, "LOG_PATH", str(log_file)):
            result = _read_last_error()
        assert "SQLite write failed" in result
        assert "ERROR" in result

    def test_returns_none_when_no_error_in_log(self, tmp_path):
        from health import _read_last_error
        log_file = tmp_path / "obd.log"
        log_file.write_text("2026-01-01 | INFO     | All good\n")
        from config import config
        with patch.object(config, "LOG_PATH", str(log_file)):
            assert _read_last_error() is None

    def test_returns_none_when_file_missing(self):
        from health import _read_last_error
        from config import config
        with patch.object(config, "LOG_PATH", "/nonexistent/obd.log"):
            assert _read_last_error() is None

    def test_reads_only_last_64kb(self, tmp_path):
        """The seek-to-last-64KB path is exercised when file is larger."""
        from health import _read_last_error
        log_file = tmp_path / "obd.log"

        # Write >64KB of INFO lines followed by one ERROR at the end
        big_prefix = ("2026-01-01 | INFO     | padding line\n" * 2000)  # ~72KB
        log_file.write_bytes(
            big_prefix.encode() +
            b"2026-01-01 | ERROR    | late error\n"
        )
        from config import config
        with patch.object(config, "LOG_PATH", str(log_file)):
            result = _read_last_error()
        assert result is not None
        assert "late error" in result

    def test_returns_most_recent_error_not_first(self, tmp_path):
        from health import _read_last_error
        log_file = tmp_path / "obd.log"
        log_file.write_text(
            "2026-01-01 | ERROR    | first error\n"
            "2026-01-01 | INFO     | recovered\n"
            "2026-01-01 | ERROR    | second error\n"
        )
        from config import config
        with patch.object(config, "LOG_PATH", str(log_file)):
            result = _read_last_error()
        assert "second error" in result


# ---------------------------------------------------------------------------
# _read_collector_version()
# ---------------------------------------------------------------------------

class TestReadCollectorVersion:
    def test_reads_version_from_file(self, tmp_path):
        from health import _read_collector_version
        ver_file = tmp_path / "VERSION"
        ver_file.write_text("1.2.3\n")
        with patch("health.VERSION_PATH", str(ver_file)):
            assert _read_collector_version() == "1.2.3"

    def test_returns_unknown_when_file_missing(self):
        from health import _read_collector_version
        with patch("health.VERSION_PATH", "/nonexistent/VERSION"):
            assert _read_collector_version() == "unknown"

    def test_strips_whitespace(self, tmp_path):
        from health import _read_collector_version
        ver_file = tmp_path / "VERSION"
        ver_file.write_text("  2.0.0  \n")
        with patch("health.VERSION_PATH", str(ver_file)):
            assert _read_collector_version() == "2.0.0"


# ---------------------------------------------------------------------------
# _check_usb_mounted()
# ---------------------------------------------------------------------------

class TestCheckUsbMounted:
    def test_returns_1_when_mounted(self):
        from health import _check_usb_mounted
        with patch("os.path.ismount", return_value=True):
            assert _check_usb_mounted() == 1

    def test_returns_0_when_not_mounted(self):
        from health import _check_usb_mounted
        with patch("os.path.ismount", return_value=False):
            assert _check_usb_mounted() == 0


# ---------------------------------------------------------------------------
# _check_bt_adapter()
# ---------------------------------------------------------------------------

class TestCheckBtAdapter:
    def test_returns_1_when_hci0_present(self):
        from health import _check_bt_adapter
        with patch("os.path.exists", return_value=True):
            assert _check_bt_adapter() == 1

    def test_returns_0_when_hci0_absent(self):
        from health import _check_bt_adapter
        with patch("os.path.exists", return_value=False):
            assert _check_bt_adapter() == 0


# ---------------------------------------------------------------------------
# _read_uptime()
# ---------------------------------------------------------------------------

class TestReadUptime:
    def test_returns_positive_integer(self):
        from health import _read_uptime
        import psutil
        with patch("psutil.boot_time", return_value=1000.0), \
             patch("health.time.time", return_value=4600.0):
            assert _read_uptime() == 3600


# ---------------------------------------------------------------------------
# collect()
# ---------------------------------------------------------------------------

class TestCollect:
    def _mock_psutil(self, mem_available_mb=512, cpu_pct=15.0, disk_free_mb=10000):
        mem = MagicMock()
        mem.available = mem_available_mb * 1024 * 1024
        disk = MagicMock()
        disk.free = disk_free_mb * 1024 * 1024
        return mem, disk, cpu_pct

    def test_collect_returns_all_keys(self, tmp_path):
        from health import collect
        log_file = tmp_path / "obd.log"
        log_file.write_text("")
        mem, disk, cpu = self._mock_psutil()
        from config import config
        with patch("psutil.virtual_memory", return_value=mem), \
             patch("psutil.cpu_percent", return_value=cpu), \
             patch("psutil.disk_usage", return_value=disk), \
             patch("psutil.boot_time", return_value=0.0), \
             patch("health.time.time", return_value=3600.0), \
             patch("os.path.ismount", return_value=True), \
             patch("os.path.exists", return_value=True), \
             patch("health.CPU_TEMP_PATH", str(tmp_path / "temp")), \
             patch.object(config, "LOG_PATH", str(log_file)):
            result = collect(obd_reconnect_count=2, restart_count=5, rtc_ok=1)

        expected_keys = {
            "cpu_temp_c", "cpu_usage_pct", "memory_free_mb", "disk_free_mb",
            "uptime_s", "usb_drive_mounted", "bt_adapter_present",
            "obd_reconnect_count", "restart_count", "rtc_ok",
            "last_error", "collector_version",
        }
        assert expected_keys <= set(result.keys())

    def test_disk_free_mb_none_when_usb_not_mounted(self, tmp_path):
        from health import collect
        log_file = tmp_path / "obd.log"
        log_file.write_text("")
        mem = MagicMock()
        mem.available = 512 * 1024 * 1024
        from config import config
        with patch("psutil.virtual_memory", return_value=mem), \
             patch("psutil.cpu_percent", return_value=5.0), \
             patch("psutil.boot_time", return_value=0.0), \
             patch("health.time.time", return_value=100.0), \
             patch("os.path.ismount", return_value=False), \
             patch("os.path.exists", return_value=False), \
             patch("health.CPU_TEMP_PATH", "/nonexistent"), \
             patch.object(config, "LOG_PATH", str(log_file)):
            result = collect(obd_reconnect_count=0, restart_count=1, rtc_ok=0)

        assert result["disk_free_mb"] is None
        assert result["usb_drive_mounted"] == 0

    def test_obd_reconnect_count_passed_through(self, tmp_path):
        from health import collect
        log_file = tmp_path / "obd.log"
        log_file.write_text("")
        mem = MagicMock()
        mem.available = 256 * 1024 * 1024
        from config import config
        with patch("psutil.virtual_memory", return_value=mem), \
             patch("psutil.cpu_percent", return_value=0.0), \
             patch("psutil.boot_time", return_value=0.0), \
             patch("health.time.time", return_value=60.0), \
             patch("os.path.ismount", return_value=False), \
             patch("os.path.exists", return_value=False), \
             patch("health.CPU_TEMP_PATH", "/nonexistent"), \
             patch.object(config, "LOG_PATH", str(log_file)):
            result = collect(obd_reconnect_count=7, restart_count=3, rtc_ok=1)

        assert result["obd_reconnect_count"] == 7
        assert result["restart_count"] == 3
        assert result["rtc_ok"] == 1

    def test_cpu_percent_uses_blocking_interval(self, tmp_path):
        """cpu_percent must take a short BLOCKING sample, never interval=None.

        collect() runs only in the one-shot sync process, which lives for a
        single call. interval=None ("usage since last call") would have no prior
        sample and return 0.0 every run, making cpu_usage_pct a dead metric. Guard
        that it is called with a positive interval so the regression can't return.
        """
        from health import collect
        log_file = tmp_path / "obd.log"
        log_file.write_text("")
        mem = MagicMock()
        mem.available = 256 * 1024 * 1024
        from config import config
        with patch("psutil.virtual_memory", return_value=mem), \
             patch("psutil.cpu_percent", return_value=42.0) as mock_cpu, \
             patch("psutil.boot_time", return_value=0.0), \
             patch("health.time.time", return_value=60.0), \
             patch("os.path.ismount", return_value=False), \
             patch("os.path.exists", return_value=False), \
             patch("health.CPU_TEMP_PATH", "/nonexistent"), \
             patch.object(config, "LOG_PATH", str(log_file)):
            result = collect(obd_reconnect_count=0, restart_count=1, rtc_ok=1)

        mock_cpu.assert_called_once()
        args, kwargs = mock_cpu.call_args
        interval = kwargs.get("interval", args[0] if args else None)
        assert interval is not None and interval > 0
        assert result["cpu_usage_pct"] == 42.0
