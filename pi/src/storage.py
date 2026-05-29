"""
storage.py — SQLite database connection and schema initialisation.

The database lives on the USB flash drive at DB_PATH. WAL mode is enabled
so that abrupt power cuts (engine off = immediate power loss) do not corrupt
the database. On next open, SQLite automatically replays the WAL file and
recovers any in-flight transaction — no manual recovery code is needed.

Schema design:
    - One table per polling tier: obd_1s, obd_5s, obd_30s
    - Separate tables for Ford Mode 22 PIDs: ford_obd_5s, ford_obd_10s, ford_obd_20s
    - Supporting tables: trips, dtc_events, pi_health_log
    - Every table has: id (UUID as TEXT), trip_id, timestamp, synced flag
    - Indexes on timestamp, trip_id, and synced for fast sync queries

The connection is opened once in main.py and passed to QueueWriter and
other modules that need database access. check_same_thread=False is set
because QueueWriter drains from a background thread, but all actual writes
are serialised through the queue — no concurrent writes occur.
"""

import sqlite3

from config import config
from logger import logger


def get_connection() -> sqlite3.Connection:
    """Open and configure the SQLite connection.

    Enables WAL journal mode and foreign key enforcement. Sets row_factory
    to sqlite3.Row so query results can be accessed by column name.

    Returns:
        Configured sqlite3.Connection instance.
    """
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    logger.info(f"SQLite connected: {config.DB_PATH}")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create all tables and indexes if they do not already exist.

    Safe to call on every startup — uses CREATE TABLE IF NOT EXISTS
    throughout. No destructive operations.

    Args:
        conn: Active SQLite connection returned by get_connection().
    """
    # TODO: Task 5 — create all tables and indexes
    pass
