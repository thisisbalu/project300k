"""Tests for main.py — check_rtc() DS3231 logic, SIGTERM handler."""

import signal
from unittest.mock import MagicMock, mock_open, patch

import pytest


# ---------------------------------------------------------------------------
# _handle_sigterm()
# ---------------------------------------------------------------------------

def test_handle_sigterm_raises_keyboard_interrupt():
    from main import _handle_sigterm
    with pytest.raises(KeyboardInterrupt):
        _handle_sigterm(signal.SIGTERM, None)


# ---------------------------------------------------------------------------
# check_rtc()
# ---------------------------------------------------------------------------

class TestCheckRtc:
    def test_returns_1_when_ds3231_present(self, caplog):
        import logging
        from main import check_rtc

        with patch("builtins.open", mock_open(read_data="ds1307\n")), \
             caplog.at_level(logging.INFO, logger="obd-collector"):
            result = check_rtc()

        assert result == 1
        assert "RTC OK" in caplog.text

    def test_returns_0_when_rtc0_not_present(self, caplog):
        import logging
        from main import check_rtc

        with patch("builtins.open", side_effect=OSError("no such file")), \
             caplog.at_level(logging.WARNING, logger="obd-collector"):
            result = check_rtc()

        assert result == 0
        assert "not found" in caplog.text

    def test_returns_0_when_unexpected_driver(self, caplog):
        import logging
        from main import check_rtc

        with patch("builtins.open", mock_open(read_data="pcf8523\n")), \
             caplog.at_level(logging.WARNING, logger="obd-collector"):
            result = check_rtc()

        assert result == 0
        assert "Unexpected RTC driver" in caplog.text

    def test_ds3231_name_variant_accepted(self):
        """Any name containing 'ds1307' is accepted — DS3231 uses ds1307 driver."""
        from main import check_rtc

        with patch("builtins.open", mock_open(read_data="ds1307\n")):
            result = check_rtc()

        assert result == 1


# ---------------------------------------------------------------------------
# wait_for_usb_mount()
# ---------------------------------------------------------------------------

class TestWaitForUsbMount:
    def test_returns_true_immediately_when_already_mounted(self):
        from main import wait_for_usb_mount

        with patch("main.os.path.ismount", return_value=True), \
             patch("main.time.sleep") as mock_sleep:
            assert wait_for_usb_mount() is True

        mock_sleep.assert_not_called()  # no polling needed when already mounted

    def test_polls_then_succeeds_when_drive_appears(self):
        from main import wait_for_usb_mount

        # Not mounted for the first two checks, then mounted.
        mount_states = iter([False, False, True])

        with patch("main.os.path.ismount", side_effect=lambda _p: next(mount_states)), \
             patch("main.time.sleep") as mock_sleep:
            assert wait_for_usb_mount() is True

        assert mock_sleep.call_count == 2  # polled twice before the drive appeared

    def test_returns_false_when_drive_never_mounts(self):
        from main import wait_for_usb_mount

        # monotonic advances past the deadline so the loop gives up.
        times = iter([0.0, 1.0, 1000.0])

        with patch("main.os.path.ismount", return_value=False), \
             patch("main.time.monotonic", side_effect=lambda: next(times)), \
             patch("main.time.sleep"):
            assert wait_for_usb_mount(timeout_s=120) is False

    def test_main_exits_when_usb_never_mounts(self):
        """main() must exit(1) — not crash later — if the drive never mounts."""
        from main import main

        with patch("main.wait_for_usb_mount", return_value=False), \
             patch("main.init_file_logging") as mock_init_log, \
             patch("main.get_connection") as mock_get_conn:
            with pytest.raises(SystemExit) as exc:
                main()

        assert exc.value.code == 1
        mock_init_log.assert_not_called()  # bailed before touching the USB log
        mock_get_conn.assert_not_called()  # and before opening the DB


# ---------------------------------------------------------------------------
# main() — full boot + watchdog loop, interrupted by KeyboardInterrupt
# ---------------------------------------------------------------------------

class TestMain:
    def test_main_runs_and_shuts_down_cleanly(self, tmp_path):
        """main() boots all components, enters watchdog loop, shuts down on KI."""
        from main import main

        def fake_sleep(n):
            raise KeyboardInterrupt

        mock_conn = MagicMock()
        mock_qw = MagicMock()
        mock_qw.write_failing = False
        mock_coll = MagicMock()
        mock_obd = MagicMock()
        mock_tm = MagicMock()

        with patch("builtins.open", mock_open(read_data="ds1307\n")), \
             patch("main.wait_for_usb_mount", return_value=True), \
             patch("main.health.write_rtc_ok"), \
             patch("main.health.increment_restart_count", return_value=1), \
             patch("main.get_connection", return_value=mock_conn), \
             patch("main.init_schema"), \
             patch("main.repair_orphaned_trips"), \
             patch("main.QueueWriter", return_value=mock_qw), \
             patch("main.OBDConnection", return_value=mock_obd), \
             patch("main.TripManager", return_value=mock_tm), \
             patch("main.Collector", return_value=mock_coll), \
             patch("main.sdnotify.SystemdNotifier"), \
             patch("main.time.sleep", side_effect=fake_sleep):

            main()

        mock_coll.stop.assert_called_once()
        mock_tm.stop.assert_called_once()
        mock_qw.stop.assert_called_once()
        mock_obd.disconnect.assert_called_once()
        mock_conn.close.assert_called_once()

    def _run_main_with_dead_worker(self, mock_qw, mock_coll):
        """Boot main() with the given (mocked) worker objects; return the notifier."""
        from main import main
        mock_conn = MagicMock()
        mock_obd = MagicMock()
        mock_tm = MagicMock()
        mock_notifier = MagicMock()

        with patch("builtins.open", mock_open(read_data="ds1307\n")), \
             patch("main.wait_for_usb_mount", return_value=True), \
             patch("main.health.write_rtc_ok"), \
             patch("main.health.increment_restart_count", return_value=1), \
             patch("main.get_connection", return_value=mock_conn), \
             patch("main.init_schema"), \
             patch("main.repair_orphaned_trips"), \
             patch("main.QueueWriter", return_value=mock_qw), \
             patch("main.OBDConnection", return_value=mock_obd), \
             patch("main.TripManager", return_value=mock_tm), \
             patch("main.Collector", return_value=mock_coll), \
             patch("main.sdnotify.SystemdNotifier", return_value=mock_notifier), \
             patch("main.time.sleep") as mock_sleep:
            main()
        return mock_notifier, mock_sleep, mock_conn

    def test_main_exits_without_pinging_when_queue_writer_dead(self):
        """A dead drain thread must stop watchdog pings so systemd restarts us."""
        mock_qw = MagicMock()
        mock_qw.is_alive = False
        mock_qw.write_failing = False
        mock_coll = MagicMock()
        mock_coll.is_monitor_alive.return_value = True

        notifier, mock_sleep, mock_conn = self._run_main_with_dead_worker(mock_qw, mock_coll)

        pinged = [c.args for c in notifier.notify.call_args_list]
        assert ("WATCHDOG=1",) not in pinged   # never pinged the watchdog
        mock_sleep.assert_not_called()          # broke before sleeping
        mock_qw.stop.assert_called_once()       # clean shutdown still ran
        mock_conn.close.assert_called_once()

    def test_main_exits_without_pinging_when_writes_failing(self):
        """A live drain thread whose flushes all fail (USB unmounted) must exit."""
        mock_qw = MagicMock()
        mock_qw.is_alive = True
        mock_qw.write_failing = True
        mock_coll = MagicMock()
        mock_coll.is_monitor_alive.return_value = True

        notifier, mock_sleep, mock_conn = self._run_main_with_dead_worker(mock_qw, mock_coll)

        pinged = [c.args for c in notifier.notify.call_args_list]
        assert ("WATCHDOG=1",) not in pinged   # withheld ping so systemd restarts
        mock_sleep.assert_not_called()          # broke before sleeping
        mock_qw.stop.assert_called_once()       # clean shutdown still ran
        mock_conn.close.assert_called_once()

    def test_main_exits_without_pinging_when_monitor_dead(self):
        """A dead obd-monitor thread must likewise stop pings and exit."""
        mock_qw = MagicMock()
        mock_qw.is_alive = True
        mock_qw.write_failing = False
        mock_coll = MagicMock()
        mock_coll.is_monitor_alive.return_value = False

        notifier, mock_sleep, _ = self._run_main_with_dead_worker(mock_qw, mock_coll)

        pinged = [c.args for c in notifier.notify.call_args_list]
        assert ("WATCHDOG=1",) not in pinged
        mock_sleep.assert_not_called()

    def test_main_registers_sigterm_handler(self):
        """SIGTERM handler must be registered before anything else starts."""
        registered = {}

        def fake_signal(sig, handler):
            registered[sig] = handler

        with patch("main.signal.signal", side_effect=fake_signal), \
             patch("builtins.open", mock_open(read_data="ds1307\n")), \
             patch("main.wait_for_usb_mount", return_value=True), \
             patch("main.health.write_rtc_ok"), \
             patch("main.health.increment_restart_count", return_value=1), \
             patch("main.get_connection", return_value=MagicMock()), \
             patch("main.init_schema"), \
             patch("main.repair_orphaned_trips"), \
             patch("main.QueueWriter", return_value=MagicMock()), \
             patch("main.OBDConnection", return_value=MagicMock()), \
             patch("main.TripManager", return_value=MagicMock()), \
             patch("main.Collector", return_value=MagicMock()), \
             patch("main.sdnotify.SystemdNotifier"), \
             patch("main.time.sleep", side_effect=KeyboardInterrupt):

            from main import main
            main()

        assert signal.SIGTERM in registered
