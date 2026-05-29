"""
conftest.py — Shared pytest fixtures and one-time environment setup.

config._load() runs at module import time, so required env vars must be set
BEFORE any src module is imported. Setting them here at the top of conftest
(which pytest loads first) guarantees the order is correct.

logger._build_logger() checks os.path.ismount("/mnt/usb") at import time.
On the dev machine, /mnt/usb is not mounted, so logger falls back to
stderr-only — that is the expected degraded behaviour and requires no mock.
"""

import os
import sqlite3
import sys

import pytest

# Required before any src module is imported — config._load() runs at import time.
os.environ.setdefault("API_URL", "http://100.64.0.1:8080/sync")
os.environ.setdefault("API_KEY", "test-api-key-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
os.environ.setdefault("TAILSCALE_IP", "100.64.0.1")
os.environ.setdefault("DB_PATH", "/tmp/test_obd.db")
os.environ.setdefault("LOG_PATH", "/tmp/test_obd.log")
os.environ.setdefault("OBD_PORT", "/dev/rfcomm0")
os.environ.setdefault("SYNC_BATCH_SIZE", "500")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.fixture
def db_conn(tmp_path):
    """SQLite connection with full schema initialised on a real temp file."""
    from storage import init_schema

    conn = sqlite3.connect(str(tmp_path / "test.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    init_schema(conn)
    yield conn
    conn.close()


@pytest.fixture
def trip_row(db_conn):
    """Insert a minimal trips row and return its UUID."""
    import uuid
    trip_id = str(uuid.uuid4())
    db_conn.execute(
        "INSERT INTO trips (id, trip_number, start_time, synced) VALUES (?, 1, '2026-01-01T00:00:00+00:00', 0)",
        (trip_id,),
    )
    db_conn.commit()
    return trip_id
