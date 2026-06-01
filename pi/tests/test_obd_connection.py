"""Tests for obd_connection.py — connect retry, reconnect, disconnect, properties."""

from unittest.mock import MagicMock, call, patch

import pytest


@pytest.fixture
def obd_conn():
    from obd_connection import OBDConnection
    return OBDConnection()


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------

def test_initial_state(obd_conn):
    assert obd_conn._connection is None
    assert obd_conn.reconnect_count == 0
    assert obd_conn.is_connected is False
    assert obd_conn.connection is None


# ---------------------------------------------------------------------------
# connect()
# ---------------------------------------------------------------------------

class TestConnect:
    def test_connects_on_first_attempt(self, obd_conn):
        mock_obd = MagicMock()
        mock_obd.is_connected.return_value = True
        mock_obd.protocol_name.return_value = "ISO 15765-4"

        with patch("obd_connection.obd.OBD", return_value=mock_obd):
            obd_conn.connect()

        # Connection is verified then closed immediately — Collector owns the port.
        mock_obd.close.assert_called_once()
        assert obd_conn._connection is None

    def test_uses_fast_false(self, obd_conn):
        mock_obd = MagicMock()
        mock_obd.is_connected.return_value = True
        mock_obd.protocol_name.return_value = "ISO 15765-4"

        with patch("obd_connection.obd.OBD", return_value=mock_obd) as mock_cls:
            obd_conn.connect()

        _, kwargs = mock_cls.call_args
        assert kwargs.get("fast") is False

    def test_retries_on_failure_then_succeeds(self, obd_conn):
        good_obd = MagicMock()
        good_obd.is_connected.return_value = True
        good_obd.protocol_name.return_value = "ISO 15765-4"

        bad_obd = MagicMock()
        bad_obd.is_connected.return_value = False

        call_count = 0

        def make_obd(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("not ready")
            return good_obd

        with patch("obd_connection.obd.OBD", side_effect=make_obd), \
             patch("obd_connection.time.sleep"):
            obd_conn.connect()

        good_obd.close.assert_called_once()
        assert obd_conn._connection is None
        assert call_count == 3

    def test_raises_keyboard_interrupt_through_retry(self, obd_conn):
        with patch("obd_connection.obd.OBD", side_effect=KeyboardInterrupt), \
             patch("obd_connection.time.sleep"):
            with pytest.raises(KeyboardInterrupt):
                obd_conn.connect()

    def test_closes_connection_when_not_connected(self, obd_conn):
        """If OBD() returns but is_connected() is False, close() is called."""
        bad_obd = MagicMock()
        bad_obd.is_connected.return_value = False

        good_obd = MagicMock()
        good_obd.is_connected.return_value = True
        good_obd.protocol_name.return_value = "ISO 15765-4"

        call_count = 0

        def make_obd(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return bad_obd if call_count == 1 else good_obd

        with patch("obd_connection.obd.OBD", side_effect=make_obd), \
             patch("obd_connection.time.sleep"):
            obd_conn.connect()

        bad_obd.close.assert_called_once()


# ---------------------------------------------------------------------------
# disconnect()
# ---------------------------------------------------------------------------

class TestDisconnect:
    def test_closes_connection(self, obd_conn):
        mock_obd = MagicMock()
        obd_conn._connection = mock_obd
        obd_conn.disconnect()
        mock_obd.close.assert_called_once()
        assert obd_conn._connection is None

    def test_noop_when_no_connection(self, obd_conn):
        obd_conn.disconnect()  # must not raise

    def test_logs_warning_on_close_error(self, obd_conn, caplog):
        import logging
        mock_obd = MagicMock()
        mock_obd.close.side_effect = Exception("rfcomm error")
        obd_conn._connection = mock_obd
        with caplog.at_level(logging.WARNING, logger="obd-collector"):
            obd_conn.disconnect()
        assert "Error closing OBD connection" in caplog.text
        assert obd_conn._connection is None


# ---------------------------------------------------------------------------
# reconnect()
# ---------------------------------------------------------------------------

class TestReconnect:
    def test_increments_reconnect_count(self, obd_conn):
        with patch.object(obd_conn, "disconnect"), \
             patch.object(obd_conn, "connect"):
            obd_conn.reconnect()
        assert obd_conn.reconnect_count == 1

    def test_calls_disconnect_then_connect(self, obd_conn):
        calls = []
        with patch.object(obd_conn, "disconnect", side_effect=lambda: calls.append("disconnect")), \
             patch.object(obd_conn, "connect", side_effect=lambda: calls.append("connect")):
            obd_conn.reconnect()
        assert calls == ["disconnect", "connect"]


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

class TestProperties:
    def test_is_connected_true_when_connection_active(self, obd_conn):
        mock_obd = MagicMock()
        mock_obd.is_connected.return_value = True
        obd_conn._connection = mock_obd
        assert obd_conn.is_connected is True

    def test_is_connected_false_when_connection_returns_false(self, obd_conn):
        mock_obd = MagicMock()
        mock_obd.is_connected.return_value = False
        obd_conn._connection = mock_obd
        assert obd_conn.is_connected is False

    def test_connection_property_returns_underlying_object(self, obd_conn):
        mock_obd = MagicMock()
        obd_conn._connection = mock_obd
        assert obd_conn.connection is mock_obd
