-- 0001_init.sql — raw tables, mirroring the Pi's SQLite schema (pi/src/storage.py).
--
-- Two deliberate deviations from the SQLite schema:
--   1. No `synced` column — that is Pi-local sync bookkeeping with no server
--      meaning. The API strips it from incoming rows.
--   2. Postgres-native types: uuid ids, timestamptz timestamps, integer counts,
--      double precision reals (SQLite stored everything as TEXT/REAL/INTEGER).
--
-- trip_id carries a FK to trips(id). The Pi syncs trips first, so a child's
-- parent is normally already present; a rare orphan batch is rejected by the
-- API (non-2xx) and self-heals on the next sync.

CREATE TABLE trips (
    id                UUID PRIMARY KEY,
    trip_number       INTEGER,
    start_time        TIMESTAMPTZ NOT NULL,
    end_time          TIMESTAMPTZ,
    start_odometer_km DOUBLE PRECISION,
    end_odometer_km   DOUBLE PRECISION,
    distance_km       DOUBLE PRECISION,   -- computed server-side later (deferred)
    duration_s        INTEGER,
    collector_version TEXT
);

CREATE TABLE obd_1s (
    id           UUID PRIMARY KEY,
    trip_id      UUID NOT NULL REFERENCES trips(id),
    timestamp    TIMESTAMPTZ NOT NULL,
    rpm          INTEGER,
    speed_kmh    INTEGER,
    throttle_pct DOUBLE PRECISION,
    load_pct     DOUBLE PRECISION
);

CREATE TABLE obd_5s (
    id                 UUID PRIMARY KEY,
    trip_id            UUID NOT NULL REFERENCES trips(id),
    timestamp          TIMESTAMPTZ NOT NULL,
    coolant_temp_c     DOUBLE PRECISION,
    oil_temp_c         DOUBLE PRECISION,
    intake_air_temp_c  DOUBLE PRECISION,
    maf_gs             DOUBLE PRECISION,
    map_kpa            DOUBLE PRECISION,
    baro_pressure_kpa  DOUBLE PRECISION,
    stft_pct           DOUBLE PRECISION,
    ltft_pct           DOUBLE PRECISION,
    o2_b1s1_v          DOUBLE PRECISION,
    o2_b1s2_v          DOUBLE PRECISION,
    timing_advance_deg DOUBLE PRECISION,
    fuel_rail_kpa      DOUBLE PRECISION
);

CREATE TABLE obd_30s (
    id                            UUID PRIMARY KEY,
    trip_id                       UUID NOT NULL REFERENCES trips(id),
    timestamp                     TIMESTAMPTZ NOT NULL,
    battery_v                     DOUBLE PRECISION,
    fuel_level_pct                DOUBLE PRECISION,
    ambient_air_temp_c            DOUBLE PRECISION,
    distance_since_dtc_cleared_km DOUBLE PRECISION
);

CREATE TABLE ford_obd_5s (
    id                      UUID PRIMARY KEY,
    trip_id                 UUID NOT NULL REFERENCES trips(id),
    timestamp               TIMESTAMPTZ NOT NULL,
    trans_temp_c            DOUBLE PRECISION,
    trans_oil_temp2_c       DOUBLE PRECISION,
    trans_line_pressure_kpa DOUBLE PRECISION,
    trans_gear              INTEGER,
    tcc_ratio               DOUBLE PRECISION
);

CREATE TABLE ford_obd_10s (
    id                UUID PRIMARY KEY,
    trip_id           UUID NOT NULL REFERENCES trips(id),
    timestamp         TIMESTAMPTZ NOT NULL,
    oil_pressure_kpa  DOUBLE PRECISION,
    knock_retard_deg  DOUBLE PRECISION,
    boost_desired_psi DOUBLE PRECISION,
    boost_actual_psi  DOUBLE PRECISION,
    cac_temp_c        DOUBLE PRECISION,
    wastegate_pct     DOUBLE PRECISION,
    vct_intake_deg    DOUBLE PRECISION,
    vct_exhaust_deg   DOUBLE PRECISION
);

CREATE TABLE ford_obd_20s (
    id               UUID PRIMARY KEY,
    trip_id          UUID NOT NULL REFERENCES trips(id),
    timestamp        TIMESTAMPTZ NOT NULL,
    misfire_acc_cyl1 INTEGER,
    misfire_acc_cyl2 INTEGER,
    misfire_acc_cyl3 INTEGER,
    misfire_acc_cyl4 INTEGER
);

CREATE TABLE dtc_events (
    id           UUID PRIMARY KEY,
    trip_id      UUID NOT NULL REFERENCES trips(id),
    timestamp    TIMESTAMPTZ NOT NULL,
    code         TEXT NOT NULL,
    description  TEXT,
    status       TEXT,
    scan_trigger TEXT
);

CREATE TABLE pi_health_log (
    id                  UUID PRIMARY KEY,
    timestamp           TIMESTAMPTZ NOT NULL,
    cpu_temp_c          DOUBLE PRECISION,
    cpu_usage_pct       DOUBLE PRECISION,
    memory_free_mb      DOUBLE PRECISION,
    disk_free_mb        DOUBLE PRECISION,
    uptime_s            BIGINT,
    usb_drive_mounted   INTEGER,
    bt_adapter_present  INTEGER,
    obd_reconnect_count INTEGER,
    restart_count       INTEGER,
    rtc_ok              INTEGER,
    last_error          TEXT,
    rows_collected      INTEGER,
    collector_version   TEXT
);

-- Indexes for trip-scoped and time-range queries (Grafana + Claude pre-aggregation).
CREATE INDEX idx_obd_1s_trip_id        ON obd_1s(trip_id);
CREATE INDEX idx_obd_1s_timestamp      ON obd_1s(timestamp);
CREATE INDEX idx_obd_5s_trip_id        ON obd_5s(trip_id);
CREATE INDEX idx_obd_5s_timestamp      ON obd_5s(timestamp);
CREATE INDEX idx_obd_30s_trip_id       ON obd_30s(trip_id);
CREATE INDEX idx_obd_30s_timestamp     ON obd_30s(timestamp);
CREATE INDEX idx_ford_obd_5s_trip_id   ON ford_obd_5s(trip_id);
CREATE INDEX idx_ford_obd_5s_timestamp ON ford_obd_5s(timestamp);
CREATE INDEX idx_ford_obd_10s_trip_id  ON ford_obd_10s(trip_id);
CREATE INDEX idx_ford_obd_10s_timestamp ON ford_obd_10s(timestamp);
CREATE INDEX idx_ford_obd_20s_trip_id  ON ford_obd_20s(trip_id);
CREATE INDEX idx_ford_obd_20s_timestamp ON ford_obd_20s(timestamp);
CREATE INDEX idx_dtc_events_trip_id    ON dtc_events(trip_id);
CREATE INDEX idx_dtc_events_timestamp  ON dtc_events(timestamp);
CREATE INDEX idx_dtc_events_code       ON dtc_events(code);
CREATE INDEX idx_trips_start_time      ON trips(start_time);
CREATE INDEX idx_pi_health_log_timestamp ON pi_health_log(timestamp);
