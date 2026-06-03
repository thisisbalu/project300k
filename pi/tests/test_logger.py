"""Tests for logger.py — file handler creation, USB-not-mounted fallback."""

import logging
import sys
from unittest.mock import MagicMock, patch

import pytest


def _reload_logger():
    """Force logger module to re-execute _build_logger() and return the module."""
    # Remove cached module and all its handlers from Python's logging registry
    existing = logging.getLogger("obd-collector")
    existing.handlers.clear()
    sys.modules.pop("logger", None)
    import logger as lg
    return lg


# ---------------------------------------------------------------------------
# USB not mounted — falls back to stderr only (default on dev machine)
# ---------------------------------------------------------------------------

class TestNoUsbMount:
    def test_stderr_handler_present_on_import(self):
        # The stderr handler is attached at import so the logger always works,
        # in any process, before init_file_logging() is called.
        lg = _reload_logger()
        stderr_handlers = [h for h in lg.logger.handlers if isinstance(h, logging.StreamHandler)
                           and not isinstance(h, logging.FileHandler)]
        assert len(stderr_handlers) >= 1

    def test_no_file_handler_when_usb_not_mounted(self):
        lg = _reload_logger()
        with patch("os.path.ismount", return_value=False):
            lg.init_file_logging()
        file_handlers = [h for h in lg.logger.handlers if isinstance(h, logging.FileHandler)]
        assert len(file_handlers) == 0

    def test_stderr_handler_level_is_warning(self):
        lg = _reload_logger()
        stderr_handlers = [h for h in lg.logger.handlers if isinstance(h, logging.StreamHandler)
                           and not isinstance(h, logging.FileHandler)]
        assert stderr_handlers[0].level == logging.WARNING


# ---------------------------------------------------------------------------
# USB mounted — file handler added
# ---------------------------------------------------------------------------

class TestUsbMounted:
    def test_file_handler_added_when_usb_mounted(self, tmp_path):
        from config import config
        log_path = str(tmp_path / "obd.log")

        lg = _reload_logger()
        with patch("os.path.ismount", return_value=True), \
             patch("os.makedirs"), \
             patch.object(config, "LOG_PATH", log_path):
            lg.init_file_logging()

        file_handlers = [h for h in lg.logger.handlers
                         if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert len(file_handlers) == 1

    def test_file_handler_level_is_debug(self, tmp_path):
        from config import config
        import logging.handlers
        log_path = str(tmp_path / "obd.log")

        lg = _reload_logger()
        with patch("os.path.ismount", return_value=True), \
             patch("os.makedirs"), \
             patch.object(config, "LOG_PATH", log_path):
            lg.init_file_logging()

        fh = [h for h in lg.logger.handlers
              if isinstance(h, logging.handlers.RotatingFileHandler)][0]
        assert fh.level == logging.DEBUG

    def test_makedirs_called_with_log_dir(self, tmp_path):
        from config import config
        log_path = str(tmp_path / "logs" / "obd.log")
        log_dir = str(tmp_path / "logs")

        lg = _reload_logger()
        with patch("os.path.ismount", return_value=True), \
             patch("os.makedirs") as mock_mkdirs, \
             patch.object(config, "LOG_PATH", log_path):
            lg.init_file_logging()

        mock_mkdirs.assert_called_once_with(log_dir, exist_ok=True)


# ---------------------------------------------------------------------------
# Sync process — stderr/journald only, never the shared USB file
# ---------------------------------------------------------------------------

class TestSyncLogging:
    def test_sync_does_not_add_file_handler(self, tmp_path):
        from config import config
        log_path = str(tmp_path / "obd.log")

        lg = _reload_logger()
        with patch("os.path.ismount", return_value=True), \
             patch.object(config, "LOG_PATH", log_path):
            lg.configure_sync_logging()  # sync must NOT open the rotating file

        file_handlers = [h for h in lg.logger.handlers if isinstance(h, logging.FileHandler)]
        assert len(file_handlers) == 0

    def test_sync_lowers_stderr_to_info(self):
        lg = _reload_logger()
        lg.configure_sync_logging()
        stderr_handlers = [h for h in lg.logger.handlers if isinstance(h, logging.StreamHandler)
                           and not isinstance(h, logging.FileHandler)]
        assert stderr_handlers[0].level == logging.INFO


# ---------------------------------------------------------------------------
# UTC timestamps — match the ISO8601 UTC timestamps stored in SQLite
# ---------------------------------------------------------------------------

class TestUtcTimestamps:
    def test_formatter_uses_utc(self):
        import time
        lg = _reload_logger()
        assert lg._FORMATTER.converter is time.gmtime
