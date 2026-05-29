import logging
import sys
from logging.handlers import RotatingFileHandler
from config import config

_logger = None


def get_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger

    _logger = logging.getLogger("obd-collector")
    _logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S"
    )

    file_handler = RotatingFileHandler(
        filename=config.LOG_PATH,
        maxBytes=5 * 1024 * 1024,  # 5MB
        backupCount=7
    )
    file_handler.setFormatter(formatter)
    _logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    _logger.addHandler(console_handler)

    _logger.info(f"Pi boot — Python {sys.version.split()[0]}")

    return _logger


logger = get_logger()
