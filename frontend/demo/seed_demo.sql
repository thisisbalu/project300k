-- seed_demo.sql — synthetic data for a PUBLIC demo instance (DEMO_MODE=true).
--
-- Generates believable-but-fake trips with the OBD rows the views need, so every
-- frontend page renders without any real vehicle data. Safe to run repeatedly:
-- it TRUNCATEs first. NEVER run this against the real database.
--
-- Apply against a demo DB:  psql "$DEMO_DATABASE_URL" -f demo/seed_demo.sql

BEGIN;

TRUNCATE trips, obd_1s, obd_5s, obd_30s,
         ford_obd_5s, ford_obd_10s, ford_obd_20s, dtc_events, pi_health_log CASCADE;

-- 40 trips over the last ~80 days, ~every other day.
WITH t AS (
  SELECT gen_random_uuid() AS id,
         n AS trip_number,
         now() - ((80 - n*2) || ' days')::interval - interval '40 minutes' AS start_time,
         now() - ((80 - n*2) || ' days')::interval                          AS end_time
  FROM generate_series(1, 40) AS n
)
INSERT INTO trips (id, trip_number, start_time, end_time, duration_s, collector_version)
SELECT id, trip_number, start_time, end_time, 2400, 'demo-1.0' FROM t;

-- obd_1s: one row/sec for the first 600s of each trip, speed as a smooth-ish wave
-- so trip_summary integrates a realistic distance.
INSERT INTO obd_1s (id, trip_id, timestamp, rpm, speed_kmh, throttle_pct, load_pct)
SELECT gen_random_uuid(), tr.id, tr.start_time + (s || ' seconds')::interval,
       1200 + (random()*1800)::int,
       GREATEST(0, (45 + 35*sin(s/60.0) + (random()*10-5)))::int,
       20 + random()*40, 25 + random()*45
FROM trips tr, generate_series(0, 600) AS s;

-- obd_5s: coolant warms to ~95C; oil left NULL (no PID, as in production); intake.
INSERT INTO obd_5s (id, trip_id, timestamp, coolant_temp_c, oil_temp_c, intake_air_temp_c, maf_gs, map_kpa, stft_pct, ltft_pct)
SELECT gen_random_uuid(), tr.id, tr.start_time + (s || ' seconds')::interval,
       LEAST(98, 40 + s*0.12 + random()*3), NULL, 22 + random()*8,
       5 + random()*15, 30 + random()*60, random()*4-2, random()*4-2
FROM trips tr, generate_series(0, 600, 5) AS s;

-- obd_30s: battery ~14.1V running (healthy charging).
INSERT INTO obd_30s (id, trip_id, timestamp, battery_v, fuel_level_pct, ambient_air_temp_c)
SELECT gen_random_uuid(), tr.id, tr.start_time + (s || ' seconds')::interval,
       13.9 + random()*0.5, 30 + random()*60, 15 + random()*12
FROM trips tr, generate_series(0, 600, 30) AS s;

-- ford_obd_5s: transmission temp climbs to ~95C.
INSERT INTO ford_obd_5s (id, trip_id, timestamp, trans_temp_c, trans_oil_temp2_c, trans_line_pressure_kpa, trans_gear, tcc_ratio)
SELECT gen_random_uuid(), tr.id, tr.start_time + (s || ' seconds')::interval,
       LEAST(100, 45 + s*0.10 + random()*3), LEAST(95, 42 + s*0.09), 1200 + random()*400,
       (1 + (s/120) % 8)::int, 0.9 + random()*0.1
FROM trips tr, generate_series(0, 600, 5) AS s;

-- A couple of historical DTCs on one older trip, to populate the Faults page.
INSERT INTO dtc_events (id, trip_id, timestamp, code, description, status, scan_trigger)
SELECT gen_random_uuid(), tr.id, tr.start_time + interval '120 seconds',
       v.code, v.descr, 'historic', 'trip_start'
FROM (SELECT id, start_time FROM trips ORDER BY start_time LIMIT 1) tr,
     (VALUES ('P0133', 'O2 Sensor Circuit Slow Response (Bank 1, Sensor 1)'),
             ('P0455', 'Evaporative Emission System Leak Detected (large)')) AS v(code, descr);

-- A recent pi_health_log row so the overview's "sync freshness" shows fresh.
INSERT INTO pi_health_log (id, timestamp, cpu_temp_c, cpu_usage_pct, memory_free_mb, disk_free_mb,
                           uptime_s, usb_drive_mounted, bt_adapter_present, obd_reconnect_count,
                           restart_count, rtc_ok, rows_collected, collector_version)
VALUES (gen_random_uuid(), now() - interval '20 minutes', 52.0, 18.0, 420.0, 12000.0,
        3600, 1, 1, 0, 1, 1, 1800, 'demo-1.0');

COMMIT;
