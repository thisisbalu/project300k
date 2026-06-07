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

import os
import sqlite3

from config import config
from logger import logger

# Increment this when the schema changes — triggers migration logic.
SCHEMA_VERSION = 4


def get_connection(_depth: int = 0) -> sqlite3.Connection:
    """Open and configure the SQLite connection.

    Enables WAL journal mode and foreign key enforcement. Sets row_factory
    to sqlite3.Row so query results can be accessed by column name.

    On integrity check failure, renames the corrupt DB to .corrupt and opens
    a fresh empty database. _depth guards against infinite recursion if the
    rename fails or the new database itself fails integrity check (drive fault).

    Args:
        _depth: Internal recursion counter — callers must not set this.

    Returns:
        Configured sqlite3.Connection instance.

    Raises:
        RuntimeError: If integrity check fails twice (drive likely failing).
        OSError:      If the corrupt DB cannot be renamed.
    """
    if _depth >= 2:
        raise RuntimeError(
            "SQLite integrity check failed twice — USB drive may be failing. "
            "Replace the drive and restore from pg_dump backup."
        )

    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row

    # WAL mode is critical for power-cut safety — on abrupt power loss (engine off),
    # SQLite replays the WAL file on next open and recovers in-flight transactions.
    # FAT32 USB drives silently fall back to DELETE journal if WAL is unavailable —
    # check the return value and warn loudly so the operator knows the risk.
    mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if mode != "wal":
        logger.warning(
            f"WAL mode not available on this filesystem (got '{mode}') — "
            "database is at risk of corruption on power cut. "
            "Format the USB drive as ext4."
        )
    else:
        logger.info("SQLite WAL mode active")

    conn.execute("PRAGMA foreign_keys=ON")

    # NORMAL synchronous level is safe with WAL — WAL checkpoints are atomic
    # even at NORMAL. Reduces fsync frequency compared to FULL, cutting USB
    # write amplification significantly on flash storage.
    conn.execute("PRAGMA synchronous=NORMAL")

    # 8MB page cache reduces USB read traffic during sync script SELECT queries.
    # Negative value = size in KiB (8000 KiB = ~8MB). Safe on Pi 3B's 1GB RAM.
    conn.execute("PRAGMA cache_size=-8000")

    # Integrity check on every open — detects corruption from a power cut that
    # WAL recovery could not fully repair. On failure, the corrupt database is
    # renamed to .corrupt and a fresh empty database is started rather than
    # crash-looping on every boot with an unrecoverable OperationalError.
    result = conn.execute("PRAGMA integrity_check").fetchone()
    if result[0] != "ok":
        logger.error(
            f"SQLite integrity check failed: {result[0]} — "
            "renaming corrupt DB and starting fresh"
        )
        conn.close()
        _quarantine_corrupt_db()
        # Recurse to open a fresh database. _depth prevents infinite recursion
        # if the drive is failing and every new file also fails integrity check.
        return get_connection(_depth=_depth + 1)

    logger.info(f"SQLite connected: {config.DB_PATH}")
    return conn


def _quarantine_corrupt_db() -> None:
    """Move a corrupt database and its WAL/SHM sidecars out of the way.

    The -wal and -shm files MUST be moved alongside the main DB. Renaming only
    obd.db leaves the orphaned -wal, which SQLite replays into the freshly
    created database on next open — re-introducing the corruption, failing the
    next integrity check, and turning recovery into a boot crash-loop (the
    recursive open hits _depth>=2 and raises). The sidecars are renamed, not
    deleted, so the corrupt data can be salvaged manually if needed.

    Raises:
        OSError: If any corrupt file cannot be moved — starting fresh is unsafe
                 while an orphaned -wal could be replayed into the new DB.
    """
    suffix = ".corrupt"
    moves = (
        (config.DB_PATH, config.DB_PATH + suffix),
        (config.DB_PATH + "-wal", config.DB_PATH + suffix + "-wal"),
        (config.DB_PATH + "-shm", config.DB_PATH + suffix + "-shm"),
    )
    for src, dst in moves:
        if not os.path.exists(src):
            continue
        try:
            os.replace(src, dst)
        except OSError as e:
            logger.error(f"Could not move corrupt DB file {src} to {dst}: {e}")
            raise
    logger.warning(f"Corrupt DB quarantined to {config.DB_PATH + suffix}*")


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


def get_trip_number(conn: sqlite3.Connection) -> int:
    """Return the next sequential trip number.

    Counts existing trips and returns count + 1. Called at trip start
    to populate the human-friendly trip_number column.

    This is safe because trips are only created one at a time (TripManager
    holds _lock during _start_trip), so there is no concurrent-insert race.

    Args:
        conn: Active SQLite connection.

    Returns:
        Next trip number (1-based).
    """
    try:
        row = conn.execute("SELECT COUNT(*) FROM trips").fetchone()
        return (row[0] or 0) + 1
    except sqlite3.Error as e:
        logger.warning(f"Could not get trip number: {e} — defaulting to 0")
        return 0


def update_trip_end(queue_writer, trip_id: str, end_time: str) -> None:
    """Write end_time and duration_s to an existing trips row.

    Called by TripManager._end_trip() via QueueWriter.direct_execute() so
    the UPDATE is serialised against the writer thread's INSERT batches.
    Using direct_execute() (rather than conn.execute() directly) acquires
    _db_lock and prevents concurrent SQLite access from two threads.

    Args:
        queue_writer: QueueWriter — provides direct_execute() and the lock.
        trip_id:      UUID of the trip to update.
        end_time:     ISO8601 UTC end timestamp.
    """
    try:
        queue_writer.direct_execute(
            """UPDATE trips
               SET end_time = ?,
                   duration_s = CAST(
                       (julianday(?) - julianday(start_time)) * 86400 AS INTEGER
                   ),
                   synced = 0
               WHERE id = ?""",
            (end_time, end_time, trip_id),
        )
        logger.info(f"Trip end written: {trip_id}")
    except Exception as e:
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
            fuel_rail_kpa         REAL,   -- GDI fuel rail pressure kPa (PID 0x23)
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

        -- ford_obd_5s — Ford Mode 22 TCM: transmission temp, gear, TCC ratio.
        -- Addresses confirmed via pid_log_20260605_190444.txt.
        CREATE TABLE IF NOT EXISTS ford_obd_5s (
            id           TEXT PRIMARY KEY,
            trip_id      TEXT NOT NULL REFERENCES trips(id),
            timestamp    TEXT NOT NULL,
            trans_temp_c REAL,    -- transmission fluid °C  (Mode 22 0x221E1C)
            trans_gear   INTEGER, -- current gear            (Mode 22 0x221E12)
            tcc_ratio    REAL,    -- TCC clutch ratio        (Mode 22 0x221E15)
            synced       INTEGER DEFAULT 0
        );

        -- ford_obd_10s — Ford Mode 22 PCM: boost, knock, VCT, oil pressure.
        -- Addresses confirmed via pid_log_20260605_190444.txt.
        CREATE TABLE IF NOT EXISTS ford_obd_10s (
            id                TEXT PRIMARY KEY,
            trip_id           TEXT NOT NULL REFERENCES trips(id),
            timestamp         TEXT NOT NULL,
            oil_pressure_kpa  REAL, -- engine oil pressure kPa    (Mode 22 0x220415)
            knock_retard_deg  REAL, -- timing retard from knock °  (Mode 22 0x2203EC)
            boost_desired_psi REAL, -- requested boost PSI         (Mode 22 0x220461)
            boost_actual_psi  REAL, -- measured boost PSI          (Mode 22 0x220462)
            cac_temp_c        REAL, -- charge air cooler temp °C   (Mode 22 0x2203CA)
            wastegate_pct     REAL, -- wastegate duty cycle %      (Mode 22 0x2203E3)
            vct_intake_deg    REAL, -- VCT intake position °        (Mode 22 0x220303)
            vct_exhaust_deg   REAL, -- VCT exhaust position °       (Mode 22 0x220304, BASE provisional)
            synced            INTEGER DEFAULT 0
        );

        -- ford_obd_20s — Mode 06 PCM: misfire accumulators per cylinder.
        -- Addresses confirmed 2026-06-06: TIDs 06A2–06A5, OBDMID 0x0B = accumulator.
        CREATE TABLE IF NOT EXISTS ford_obd_20s (
            id                TEXT PRIMARY KEY,
            trip_id           TEXT NOT NULL REFERENCES trips(id),
            timestamp         TEXT NOT NULL,
            misfire_acc_cyl1  INTEGER, -- cumulative misfire count cyl1 (Mode 06 TID 06A2)
            misfire_acc_cyl2  INTEGER, -- cumulative misfire count cyl2 (Mode 06 TID 06A3)
            misfire_acc_cyl3  INTEGER, -- cumulative misfire count cyl3 (Mode 06 TID 06A4)
            misfire_acc_cyl4  INTEGER, -- cumulative misfire count cyl4 (Mode 06 TID 06A5)
            synced            INTEGER DEFAULT 0
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

        -- ford_obd_5s indexes
        CREATE INDEX IF NOT EXISTS idx_ford_obd_5s_trip_id   ON ford_obd_5s(trip_id);
        CREATE INDEX IF NOT EXISTS idx_ford_obd_5s_timestamp  ON ford_obd_5s(timestamp);
        CREATE INDEX IF NOT EXISTS idx_ford_obd_5s_synced     ON ford_obd_5s(synced);

        -- ford_obd_10s indexes
        CREATE INDEX IF NOT EXISTS idx_ford_obd_10s_trip_id   ON ford_obd_10s(trip_id);
        CREATE INDEX IF NOT EXISTS idx_ford_obd_10s_timestamp  ON ford_obd_10s(timestamp);
        CREATE INDEX IF NOT EXISTS idx_ford_obd_10s_synced     ON ford_obd_10s(synced);

        -- ford_obd_20s indexes
        CREATE INDEX IF NOT EXISTS idx_ford_obd_20s_trip_id   ON ford_obd_20s(trip_id);
        CREATE INDEX IF NOT EXISTS idx_ford_obd_20s_timestamp  ON ford_obd_20s(timestamp);
        CREATE INDEX IF NOT EXISTS idx_ford_obd_20s_synced     ON ford_obd_20s(synced);

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
