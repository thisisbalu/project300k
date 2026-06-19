-- 0002_views.sql — domain views for Grafana and Claude API.
--
-- Plain (non-materialized) views. At ~192K rows/month the raw tables query fast,
-- so there is no refresh to orchestrate. Each view is a thin single-source
-- projection — cross-cadence joins (e.g. 1s engine data against 10s boost data)
-- are deliberately avoided because the timestamps do not align; Grafana overlays
-- panels from several views on a shared time axis instead.

CREATE VIEW engine AS
    SELECT trip_id, timestamp, rpm, speed_kmh, throttle_pct, load_pct
    FROM obd_1s;

CREATE VIEW thermals AS
    SELECT trip_id, timestamp, coolant_temp_c, oil_temp_c, intake_air_temp_c
    FROM obd_5s;

CREATE VIEW fueling AS
    SELECT trip_id, timestamp,
           maf_gs, stft_pct, ltft_pct, o2_b1s1_v, o2_b1s2_v, fuel_rail_kpa
    FROM obd_5s;

CREATE VIEW boost AS
    SELECT trip_id, timestamp,
           boost_desired_psi, boost_actual_psi, wastegate_pct, knock_retard_deg,
           cac_temp_c, vct_intake_deg, vct_exhaust_deg
    FROM ford_obd_10s;

CREATE VIEW electrical AS
    SELECT trip_id, timestamp, battery_v, fuel_level_pct, ambient_air_temp_c
    FROM obd_30s;

CREATE VIEW transmission AS
    SELECT trip_id, timestamp,
           trans_temp_c, trans_oil_temp2_c, trans_line_pressure_kpa,
           trans_gear, tcc_ratio
    FROM ford_obd_5s;

CREATE VIEW misfires AS
    SELECT trip_id, timestamp,
           misfire_acc_cyl1, misfire_acc_cyl2, misfire_acc_cyl3, misfire_acc_cyl4
    FROM ford_obd_20s;
