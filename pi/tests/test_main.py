"""Tests for main.py — check_rtc() DS3231 logic, SIGTERM handler."""

import signal
from unittest.mock import MagicMock, patch

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
    def test_returns_1_when_osf_clear(self, caplog):
        import logging
        from main import check_rtc, _DS3231_I2C_ADDRESS, _DS3231_STATUS_REG, _DS3231_OSF_BIT

        mock_bus = MagicMock()
        # OSF bit (0x80) is NOT set → 0x00 means clock is running fine
        mock_bus.read_byte_data.return_value = 0x00

        with patch("main.smbus2.SMBus") as mock_smbus, \
             caplog.at_level(logging.INFO, logger="obd-collector"):
            mock_smbus.return_value.__enter__ = MagicMock(return_value=mock_bus)
            mock_smbus.return_value.__exit__ = MagicMock(return_value=False)
            result = check_rtc()

        assert result == 1
        assert "RTC OK" in caplog.text

    def test_returns_0_when_osf_set(self, caplog):
        import logging
        from main import check_rtc

        mock_bus = MagicMock()
        # OSF bit (bit 7) is set → 0x80
        mock_bus.read_byte_data.return_value = 0x80

        with patch("main.smbus2.SMBus") as mock_smbus, \
             caplog.at_level(logging.WARNING, logger="obd-collector"):
            mock_smbus.return_value.__enter__ = MagicMock(return_value=mock_bus)
            mock_smbus.return_value.__exit__ = MagicMock(return_value=False)
            result = check_rtc()

        assert result == 0
        assert "OSF flag set" in caplog.text

    def test_returns_0_when_i2c_chip_not_found(self, caplog):
        import logging
        from main import check_rtc

        with patch("main.smbus2.SMBus") as mock_smbus, \
             caplog.at_level(logging.WARNING, logger="obd-collector"):
            mock_smbus.return_value.__enter__ = MagicMock(side_effect=OSError("no device"))
            mock_smbus.return_value.__exit__ = MagicMock(return_value=False)
            result = check_rtc()

        assert result == 0
        assert "not found on I2C bus" in caplog.text

    def test_uses_context_manager_for_smbus(self):
        """SMBus must be opened via context manager so fd is closed on exception."""
        from main import check_rtc

        context_entered = []
        context_exited = []

        mock_bus = MagicMock()
        mock_bus.read_byte_data.return_value = 0x00

        class FakeSMBus:
            def __enter__(self):
                context_entered.append(True)
                return mock_bus

            def __exit__(self, *args):
                context_exited.append(True)
                return False

        with patch("main.smbus2.SMBus", return_value=FakeSMBus()):
            check_rtc()

        assert context_entered
        assert context_exited

    def test_osf_bit_other_status_bits_ignored(self):
        """Only bit 7 (OSF) matters — other status bits must not affect result."""
        from main import check_rtc

        mock_bus = MagicMock()
        # Bits 0-6 set, bit 7 clear → OSF not set → clock reliable
        mock_bus.read_byte_data.return_value = 0x7F

        with patch("main.smbus2.SMBus") as mock_smbus:
            mock_smbus.return_value.__enter__ = MagicMock(return_value=mock_bus)
            mock_smbus.return_value.__exit__ = MagicMock(return_value=False)
            result = check_rtc()

        assert result == 1


# ---------------------------------------------------------------------------
# main() — full boot + watchdog loop, interrupted by KeyboardInterrupt
# ---------------------------------------------------------------------------

class TestMain:
    def test_main_runs_and_shuts_down_cleanly(self, tmp_path):
        """main() boots all components, enters watchdog loop, shuts down on KI."""
        import itertools
        from main import main

        mock_conn = MagicMock()
        mock_bus = MagicMock()
        mock_bus.read_byte_data.return_value = 0x00  # OSF clear

        # Make time.sleep raise KeyboardInterrupt on first call to exit the loop
        sleep_calls = iter([KeyboardInterrupt()])

        def fake_sleep(n):
            exc = next(sleep_calls, None)
            if exc:
                raise exc

        mock_qw = MagicMock()
        mock_coll = MagicMock()
        mock_obd = MagicMock()
        mock_tm = MagicMock()

        with patch("main.smbus2.SMBus") as mock_smbus, \
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

            mock_smbus.return_value.__enter__ = MagicMock(return_value=mock_bus)
            mock_smbus.return_value.__exit__ = MagicMock(return_value=False)

            main()

        # Verify clean shutdown sequence including new trip_manager.stop()
        mock_coll.stop.assert_called_once()
        mock_tm.stop.assert_called_once()
        mock_qw.stop.assert_called_once()
        mock_obd.disconnect.assert_called_once()
        mock_conn.close.assert_called_once()

    def test_main_registers_sigterm_handler(self):
        """SIGTERM handler must be registered before anything else starts."""
        import signal
        registered = {}

        def fake_signal(sig, handler):
            registered[sig] = handler

        with patch("main.signal.signal", side_effect=fake_signal), \
             patch("main.smbus2.SMBus") as mock_smbus, \
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

            mock_bus = MagicMock()
            mock_bus.read_byte_data.return_value = 0x00
            mock_smbus.return_value.__enter__ = MagicMock(return_value=mock_bus)
            mock_smbus.return_value.__exit__ = MagicMock(return_value=False)

            from main import main
            main()

        assert signal.SIGTERM in registered
