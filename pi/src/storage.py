"""
storage.py — SQLite database connection and schema initialisation.

The database lives on the USB flash drive at DB_PATH. WAL mode is enabled
so that abrupt power cuts (engine off = immediate power loss) do not corrupt
the database. On next open, SQLite automatically replays the WAL file and
recovers any in-flight transaction — no manual recovery code is needed.

Schema design:
    - One table per polling tier: obd_1s, obd_5s, obd_30s
    - Ford Mode 22 tables (ford_obd_5s/10s/20s) added after FORScan scan
    - Supporting tables: trips, dtc_events, pi_health_log
    - Every table has: id (UUID as TEXT primary key), timestamp, synced flag
    - All OBD tables have trip_id (FK to trips.id)
    - Indexes on timestamp, trip_id, and synced for fast sync queries
    - schema_version table tracks applied migrations

Column naming conventions:
    - Temperatures: _c suffix (coolant_temp_c)
    - Voltages: _v suffix (battery_v)
    - Percentages: _pct suffix (throttle_pct)
    - Pressures: _kpa or _psi suffix (map_kpa)
    - Speeds: _kmh suffix (speed_kmh)
    - Flows: _gs suffix (maf_gs)

The connection is opened once in main.py and passed to QueueWriter and
other modules that need database access. check_same_thread=False is set
because QueueWriter drains from a background thread, but all actual writes
are serialised through the queue — no concurrent writes occur.
"""

import sqlite3

from config import config
from logger import logger

# Increment this when the schema changes — triggers migration logic.
SCHEMA_VERSION = 1


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
    _create_tables(conn)
    _create_indexes(conn)
    _record_schema_version(conn)
    logger.info(f"Schema initialised — version {SCHEMA_VERSION}")


def update_trip_end(conn: sqlite3.Connection, trip_id: str, end_time: str) -> None:
    """Write end_time and duration_s to an existing trips row.

    Called by TripManager._end_trip() via a direct UPDATE rather than
    going through QueueWriter INSERT, because the trips row already exists
    (written at trip start) and cannot be re-inserted with the same UUID.

    Args:
        conn:     Active SQLite connection.
        trip_id:  UUID of the trip to update.
        end_time: ISO8601 UTC end timestamp.
    """
    try:
        conn.execute(
            """UPDATE trips
               SET end_time = ?,
                   duration_s = CAST(
                       (julianday(?) - julianday(start_time)) * 86400 AS INTEGER
                   ),
                   synced = 0
               WHERE id = ?""",
            (end_time, end_time, trip_id)
        )
        conn.commit()
        logger.info(f"Trip end written: {trip_id}")
    except sqlite3.Error as e:
        logger.error(f"Failed to write trip end for {trip_id}: {e}")


def _create_tables(conn: sqlite3.Connection) -> None:
    """Create all tables if they do not already exist."""

    conn.executescript("""

        -- Schema version tracking — used to detect when migrations are needed.
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );

        -- trips — anchor table. Every OBD reading, DTC, and health row
        -- references a trip_id from here.
        CREATE TABLE IF NOT EXISTS trips (
            id                   TEXT PRIMARY KEY,
            trip_number          INTEGER,          -- human-friendly sequential ID
            start_time           TEXT NOT NULL,    -- ISO8601 UTC
            end_time             TEXT,             -- NULL until trip ends
            start_odometer_km    REAL,             -- NULL if Mode 22 odometer unavailable
            end_odometer_km      REAL,             -- NULL if Mode 22 odometer unavailable
            distance_km          REAL,             -- calculated post-sync in PostgreSQL
            duration_s           INTEGER,          -- calculated at trip end
            collector_version    TEXT,             -- version of collector that recorded this trip
            synced               INTEGER DEFAULT 0
        );

        -- obd_1s — 1 row per second. Core engine state.
        -- ~155,880 rows/month based on 1hr/day Mon-Sat + 4hrs Sunday.
        CREATE TABLE IF NOT EXISTS obd_1s (
            id           TEXT PRIMARY KEY,
            trip_id      TEXT NOT NULL REFERENCES trips(id),
            timestamp    TEXT NOT NULL,    -- ISO8601 UTC
            rpm          INTEGER,          -- 0–16383 RPM, NULL on read error
            speed_kmh    INTEGER,          -- 0–255 km/h, NULL on read error
            throttle_pct REAL,             -- 0.0–100.0 %, NULL on read error
            load_pct     REAL,             -- 0.0–100.0 %, NULL on read error
            synced       INTEGER DEFAULT 0
        );

        -- obd_5s — 1 row per 5 seconds. Thermals, air/fuel, O2 sensors.
        -- ~31,176 rows/month.
        CREATE TABLE IF NOT EXISTS obd_5s (
            id                    TEXT PRIMARY KEY,
            trip_id               TEXT NOT NULL REFERENCES trips(id),
            timestamp             TEXT NOT NULL,
            coolant_temp_c        REAL,   -- engine coolant °C
            oil_temp_c            REAL,   -- engine oil °C
            intake_air_temp_c     REAL,   -- intake air temperature °C
            maf_gs                REAL,   -- mass air flow g/s
            map_kpa               REAL,   -- manifold absolute pressure kPa
            baro_pressure_kpa     REAL,   -- barometric pressure kPa
            stft_pct              REAL,   -- short term fuel trim %
            ltft_pct              REAL,   -- long term fuel trim %
            o2_b1s1_v             REAL,   -- O2 sensor bank1 sensor1 voltage
            o2_b1s2_v             REAL,   -- O2 sensor bank1 sensor2 voltage
            timing_advance_deg    REAL,   -- ignition timing advance degrees
            synced                INTEGER DEFAULT 0
        );

        -- obd_30s — 1 row per 30 seconds. Slow-moving signals.
        -- ~5,196 rows/month.
        CREATE TABLE IF NOT EXISTS obd_30s (
            id                           TEXT PRIMARY KEY,
            trip_id                      TEXT NOT NULL REFERENCES trips(id),
            timestamp                    TEXT NOT NULL,
            battery_v                    REAL,   -- control module voltage V
            fuel_level_pct               REAL,   -- fuel tank level %
            ambient_air_temp_c           REAL,   -- outside air temperature °C
            distance_since_dtc_cleared_km REAL,  -- km since DTCs last cleared
            synced                       INTEGER DEFAULT 0
        );

        -- dtc_events — fault codes scanned at trip start and trip end.
        -- Low volume — only populated when DTCs are present.
        CREATE TABLE IF NOT EXISTS dtc_events (
            id           TEXT PRIMARY KEY,
            trip_id      TEXT NOT NULL REFERENCES trips(id),
            timestamp    TEXT NOT NULL,
            code         TEXT NOT NULL,   -- e.g. "P0300"
            description  TEXT,           -- human readable description
            status       TEXT,           -- "stored" or "pending"
            scan_trigger TEXT,           -- "trip_start" or "trip_end"
            synced       INTEGER DEFAULT 0
        );

        -- pi_health_log — one row per sync attempt.
        -- Surfaces Pi hardware issues on Grafana without needing SSH.
        CREATE TABLE IF NOT EXISTS pi_health_log (
            id                   TEXT PRIMARY KEY,
            timestamp            TEXT NOT NULL,
            cpu_temp_c           REAL,
            cpu_usage_pct        REAL,
            memory_free_mb       REAL,
            disk_free_mb         REAL,
            uptime_s             INTEGER,
            usb_drive_mounted    INTEGER,   -- 0 or 1
            bt_adapter_present   INTEGER,   -- 0 or 1
            obd_reconnect_count  INTEGER,
            restart_count        INTEGER,
            rtc_ok               INTEGER,   -- 0 or 1 — DS3231 OSF clear
            last_error           TEXT,      -- last ERROR log line, NULL if clean
            rows_collected       INTEGER,   -- unsynced rows at snapshot time
            collector_version    TEXT,
            synced               INTEGER DEFAULT 0
        );

    """)
    conn.commit()
    logger.info("Tables created (or already exist)")


def _create_indexes(conn: sqlite3.Connection) -> None:
    """Create indexes for fast sync and time-range queries."""

    conn.executescript("""

        -- obd_1s indexes
        CREATE INDEX IF NOT EXISTS idx_obd_1s_trip_id   ON obd_1s(trip_id);
        CREATE INDEX IF NOT EXISTS idx_obd_1s_timestamp  ON obd_1s(timestamp);
        CREATE INDEX IF NOT EXISTS idx_obd_1s_synced     ON obd_1s(synced);

        -- obd_5s indexes
        CREATE INDEX IF NOT EXISTS idx_obd_5s_trip_id   ON obd_5s(trip_id);
        CREATE INDEX IF NOT EXISTS idx_obd_5s_timestamp  ON obd_5s(timestamp);
        CREATE INDEX IF NOT EXISTS idx_obd_5s_synced     ON obd_5s(synced);

        -- obd_30s indexes
        CREATE INDEX IF NOT EXISTS idx_obd_30s_trip_id   ON obd_30s(trip_id);
        CREATE INDEX IF NOT EXISTS idx_obd_30s_timestamp  ON obd_30s(timestamp);
        CREATE INDEX IF NOT EXISTS idx_obd_30s_synced     ON obd_30s(synced);

        -- dtc_events indexes
        CREATE INDEX IF NOT EXISTS idx_dtc_events_trip_id   ON dtc_events(trip_id);
        CREATE INDEX IF NOT EXISTS idx_dtc_events_timestamp  ON dtc_events(timestamp);
        CREATE INDEX IF NOT EXISTS idx_dtc_events_synced     ON dtc_events(synced);
        CREATE INDEX IF NOT EXISTS idx_dtc_events_code       ON dtc_events(code);

        -- trips indexes
        CREATE INDEX IF NOT EXISTS idx_trips_start_time  ON trips(start_time);
        CREATE INDEX IF NOT EXISTS idx_trips_synced      ON trips(synced);

        -- pi_health_log indexes
        CREATE INDEX IF NOT EXISTS idx_pi_health_log_timestamp ON pi_health_log(timestamp);
        CREATE INDEX IF NOT EXISTS idx_pi_health_log_synced    ON pi_health_log(synced);

    """)
    conn.commit()
    logger.info("Indexes created (or already exist)")


def _record_schema_version(conn: sqlite3.Connection) -> None:
    """Insert the current schema version if not already recorded.

    Uses INSERT OR IGNORE so repeated startups do not create duplicate rows.
    """
    from datetime import datetime, timezone
    conn.execute(
        "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
        (SCHEMA_VERSION, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
