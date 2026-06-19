-- 0003_trip_distance.sql — derived trip distance + lifetime odometer views.
--
-- This vehicle exposes no odometer PID, so the Pi always leaves trips.distance_km
-- (and start/end_odometer_km) NULL and defers the calculation here ("calculated
-- post-sync in PostgreSQL", per pi/src/trip.py). Distance is integrated from the
-- 1 Hz speed samples in obd_1s.
--
-- Kept as plain views (no refresh to orchestrate, consistent with 0002_views.sql).
-- At ~192K rows/month obd_1s aggregates fast; revisit only if it ever drags.

-- Per-trip summary: trapezoidal integration of speed over time.
--   distance_km = Σ avg(speedₙ, speedₙ₋₁) · Δt   (Δt in hours; speed in km/h)
-- A 5 s gap cap drops intervals where sampling stalled (BT reconnect, restart) so
-- a long gap can't integrate a stale speed into phantom kilometres.
CREATE VIEW trip_summary AS
WITH samples AS (
    SELECT
        trip_id,
        speed_kmh,
        EXTRACT(EPOCH FROM (timestamp - LAG(timestamp) OVER w)) AS dt_s,
        LAG(speed_kmh) OVER w                                   AS prev_speed_kmh
    FROM obd_1s
    WHERE speed_kmh IS NOT NULL
    WINDOW w AS (PARTITION BY trip_id ORDER BY timestamp)
),
per_trip AS (
    SELECT
        trip_id,
        SUM(
            CASE WHEN dt_s > 0 AND dt_s <= 5
                 THEN ((speed_kmh + prev_speed_kmh) / 2.0) * (dt_s / 3600.0)
                 ELSE 0
            END
        )                                                AS distance_km,
        MAX(speed_kmh)                                   AS max_speed_kmh,
        AVG(speed_kmh) FILTER (WHERE speed_kmh > 0)      AS avg_moving_speed_kmh
    FROM samples
    GROUP BY trip_id
)
SELECT
    t.id                                AS trip_id,
    t.trip_number,
    t.start_time,
    t.end_time,
    t.duration_s,
    t.collector_version,
    COALESCE(p.distance_km, 0)          AS distance_km,
    p.max_speed_kmh,
    p.avg_moving_speed_kmh
FROM trips t
LEFT JOIN per_trip p ON p.trip_id = t.id;

-- Lifetime totals — the running distance logged since monitoring began. The
-- absolute odometer baseline (the dash reading on day one) is applied in Grafana
-- as a dashboard constant, so the 300,000 km progress gauge needs no schema here.
CREATE VIEW lifetime_distance AS
SELECT
    COALESCE(SUM(distance_km), 0) AS total_distance_km,
    COUNT(*)                      AS trip_count,
    MIN(start_time)              AS first_trip_at,
    MAX(start_time)              AS last_trip_at
FROM trip_summary;
