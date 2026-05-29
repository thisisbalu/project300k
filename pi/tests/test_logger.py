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
    def test_stderr_handler_always_added(self):
        with patch("os.path.ismount", return_value=False):
            lg = _reload_logger()
        handlers = lg.logger.handlers
        stderr_handlers = [h for h in handlers if isinstance(h, logging.StreamHandler)
                           and not isinstance(h, logging.FileHandler)]
        assert len(stderr_handlers) >= 1

    def test_no_file_handler_when_usb_not_mounted(self):
        with patch("os.path.ismount", return_value=False):
            lg = _reload_logger()
        file_handlers = [h for h in lg.logger.handlers if isinstance(h, logging.FileHandler)]
        assert len(file_handlers) == 0

    def test_stderr_handler_level_is_warning(self):
        with patch("os.path.ismount", return_value=False):
            lg = _reload_logger()
        handlers = lg.logger.handlers
        stderr_handlers = [h for h in handlers if isinstance(h, logging.StreamHandler)
                           and not isinstance(h, logging.FileHandler)]
        assert stderr_handlers[0].level == logging.WARNING


# ---------------------------------------------------------------------------
# USB mounted — file handler added
# ---------------------------------------------------------------------------

class TestUsbMounted:
    def test_file_handler_added_when_usb_mounted(self, tmp_path):
        from config import config
        log_path = str(tmp_path / "obd.log")

        with patch("os.path.ismount", return_value=True), \
             patch("os.makedirs"), \
             patch.object(config, "LOG_PATH", log_path):
            lg = _reload_logger()

        file_handlers = [h for h in lg.logger.handlers
                         if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert len(file_handlers) == 1

    def test_file_handler_level_is_debug(self, tmp_path):
        from config import config
        import logging.handlers
        log_path = str(tmp_path / "obd.log")

        with patch("os.path.ismount", return_value=True), \
             patch("os.makedirs"), \
             patch.object(config, "LOG_PATH", log_path):
            lg = _reload_logger()

        fh = [h for h in lg.logger.handlers
              if isinstance(h, logging.handlers.RotatingFileHandler)][0]
        assert fh.level == logging.DEBUG

    def test_makedirs_called_with_log_dir(self, tmp_path):
        from config import config
        log_path = str(tmp_path / "logs" / "obd.log")
        log_dir = str(tmp_path / "logs")

        with patch("os.path.ismount", return_value=True), \
             patch("os.makedirs") as mock_mkdirs, \
             patch.object(config, "LOG_PATH", log_path):
            _reload_logger()

        mock_mkdirs.assert_called_once_with(log_dir, exist_ok=True)
