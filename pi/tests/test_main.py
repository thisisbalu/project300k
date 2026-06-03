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
        mock_coll = MagicMock()
        mock_obd = MagicMock()
        mock_tm = MagicMock()

        with patch("builtins.open", mock_open(read_data="ds1307\n")), \
             patch("main.health.write_rtc_ok"), \
             patch("main.health.increment_restart_count", return_value=1), \
             patch("main.get_connection", return_value=mock_conn), \
             patch("main.init_schema"), \
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
             patch("main.health.write_rtc_ok"), \
             patch("main.health.increment_restart_count", return_value=1), \
             patch("main.get_connection", return_value=mock_conn), \
             patch("main.init_schema"), \
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
        mock_coll = MagicMock()
        mock_coll.is_monitor_alive.return_value = True

        notifier, mock_sleep, mock_conn = self._run_main_with_dead_worker(mock_qw, mock_coll)

        pinged = [c.args for c in notifier.notify.call_args_list]
        assert ("WATCHDOG=1",) not in pinged   # never pinged the watchdog
        mock_sleep.assert_not_called()          # broke before sleeping
        mock_qw.stop.assert_called_once()       # clean shutdown still ran
        mock_conn.close.assert_called_once()

    def test_main_exits_without_pinging_when_monitor_dead(self):
        """A dead obd-monitor thread must likewise stop pings and exit."""
        mock_qw = MagicMock()
        mock_qw.is_alive = True
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
             patch("main.health.write_rtc_ok"), \
             patch("main.health.increment_restart_count", return_value=1), \
             patch("main.get_connection", return_value=MagicMock()), \
             patch("main.init_schema"), \
             patch("main.QueueWriter", return_value=MagicMock()), \
             patch("main.OBDConnection", return_value=MagicMock()), \
             patch("main.TripManager", return_value=MagicMock()), \
             patch("main.Collector", return_value=MagicMock()), \
             patch("main.sdnotify.SystemdNotifier"), \
             patch("main.time.sleep", side_effect=KeyboardInterrupt):

            from main import main
            main()

        assert signal.SIGTERM in registered
