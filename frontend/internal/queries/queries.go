// Package queries holds every read query the frontend runs against the backend's
// PostgreSQL. Each returns plain structs the templ views render. All queries are
// read-only and lean on the views the backend already provides (trip_summary,
// lifetime_distance) plus the raw tables for per-trip peaks.
package queries

import (
	"context"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// Store is the query handle.
type Store struct{ pool *pgxpool.Pool }

func New(pool *pgxpool.Pool) *Store { return &Store{pool: pool} }

// ---- Overview -------------------------------------------------------------

// Lifetime is the running total since logging began.
type Lifetime struct {
	TotalKm    float64
	TripCount  int64
	LastTripAt *time.Time
}

func (s *Store) Lifetime(ctx context.Context) (Lifetime, error) {
	var l Lifetime
	err := s.pool.QueryRow(ctx, `
		SELECT COALESCE(total_distance_km, 0), COALESCE(trip_count, 0), last_trip_at
		FROM lifetime_distance`).Scan(&l.TotalKm, &l.TripCount, &l.LastTripAt)
	return l, err
}

// Trip is one row of the trip list / the latest-trip card.
type Trip struct {
	ID           string
	Number       *int
	StartTime    time.Time
	EndTime      *time.Time
	DurationS    *int
	DistanceKm   float64
	MaxSpeedKmh  *int
	AvgMovingKmh *float64
}

// LatestTrip returns the most recent trip, or ok=false if there are none.
func (s *Store) LatestTrip(ctx context.Context) (Trip, bool, error) {
	t, err := s.scanTrip(s.pool.QueryRow(ctx, `
		SELECT trip_id, trip_number, start_time, end_time, duration_s,
		       distance_km, max_speed_kmh, avg_moving_speed_kmh
		FROM trip_summary ORDER BY start_time DESC LIMIT 1`))
	if err != nil {
		if isNoRows(err) {
			return Trip{}, false, nil
		}
		return Trip{}, false, err
	}
	return t, true, nil
}

// Trips returns the most recent trips, newest first.
func (s *Store) Trips(ctx context.Context, limit int) ([]Trip, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT trip_id, trip_number, start_time, end_time, duration_s,
		       distance_km, max_speed_kmh, avg_moving_speed_kmh
		FROM trip_summary ORDER BY start_time DESC LIMIT $1`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Trip
	for rows.Next() {
		t, err := s.scanTrip(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, t)
	}
	return out, rows.Err()
}

type rowScanner interface{ Scan(dest ...any) error }

func (s *Store) scanTrip(r rowScanner) (Trip, error) {
	var t Trip
	err := r.Scan(&t.ID, &t.Number, &t.StartTime, &t.EndTime, &t.DurationS,
		&t.DistanceKm, &t.MaxSpeedKmh, &t.AvgMovingKmh)
	return t, err
}

// ---- Trip detail ----------------------------------------------------------

// TripPeaks are the per-trip extremes shown on the detail page.
type TripPeaks struct {
	MaxCoolantC *float64
	MaxOilC     *float64
	MaxTransC   *float64
	MinBatteryV *float64
	MaxBatteryV *float64

	// Engine-internals longevity signals.
	MinOilPressureKpa *float64
	MaxKnockRetard    *float64
	MaxBoostDesired   *float64
	MaxBoostActual    *float64
	// MisfireRatePct is misfires ÷ combustion events × 100 — drive-length
	// independent (a long drive has more events, so a raw count would mislead).
	// nil when the trip has no usable RPM data; 0 = a genuinely clean drive.
	MisfireRatePct *float64
}

func (s *Store) TripByID(ctx context.Context, id string) (Trip, bool, error) {
	t, err := s.scanTrip(s.pool.QueryRow(ctx, `
		SELECT trip_id, trip_number, start_time, end_time, duration_s,
		       distance_km, max_speed_kmh, avg_moving_speed_kmh
		FROM trip_summary WHERE trip_id = $1`, id))
	if err != nil {
		if isNoRows(err) {
			return Trip{}, false, nil
		}
		return Trip{}, false, err
	}
	return t, true, nil
}

func (s *Store) TripPeaks(ctx context.Context, id string) (TripPeaks, error) {
	var p TripPeaks
	if err := s.pool.QueryRow(ctx,
		`SELECT max(coolant_temp_c), max(oil_temp_c) FROM obd_5s WHERE trip_id=$1`, id).
		Scan(&p.MaxCoolantC, &p.MaxOilC); err != nil {
		return p, err
	}
	if err := s.pool.QueryRow(ctx,
		`SELECT max(trans_temp_c) FROM ford_obd_5s WHERE trip_id=$1`, id).
		Scan(&p.MaxTransC); err != nil {
		return p, err
	}
	if err := s.pool.QueryRow(ctx,
		`SELECT min(battery_v), max(battery_v) FROM obd_30s WHERE trip_id=$1`, id).
		Scan(&p.MinBatteryV, &p.MaxBatteryV); err != nil {
		return p, err
	}
	if err := s.pool.QueryRow(ctx,
		`SELECT min(oil_pressure_kpa), max(knock_retard_deg),
		        max(boost_desired_psi), max(boost_actual_psi)
		 FROM ford_obd_10s WHERE trip_id=$1`, id).
		Scan(&p.MinOilPressureKpa, &p.MaxKnockRetard, &p.MaxBoostDesired, &p.MaxBoostActual); err != nil {
		return p, err
	}
	// Misfire counters accumulate within a drive, so the per-cylinder max ≈ that
	// drive's total. Divide by combustion events (revolutions × 2 for a 4-cyl
	// 4-stroke; revolutions = Σrpm/60) to get a drive-length-independent rate.
	if err := s.pool.QueryRow(ctx, `
		SELECT (COALESCE(max(m.misfire_acc_cyl1),0)+COALESCE(max(m.misfire_acc_cyl2),0)
		      + COALESCE(max(m.misfire_acc_cyl3),0)+COALESCE(max(m.misfire_acc_cyl4),0))
		       / NULLIF((SELECT sum(rpm)/30.0 FROM obd_1s WHERE trip_id=$1 AND rpm IS NOT NULL), 0) * 100
		FROM ford_obd_20s m WHERE m.trip_id=$1`, id).
		Scan(&p.MisfireRatePct); err != nil {
		return p, err
	}
	return p, nil
}

// ---- DTC log --------------------------------------------------------------

type DTC struct {
	Timestamp   time.Time
	Code        string
	Description *string
	Status      *string
	ScanTrigger *string
	TripID      string
}

func (s *Store) DTCs(ctx context.Context, limit int) ([]DTC, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT timestamp, code, description, status, scan_trigger, trip_id
		FROM dtc_events ORDER BY timestamp DESC LIMIT $1`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []DTC
	for rows.Next() {
		var d DTC
		if err := rows.Scan(&d.Timestamp, &d.Code, &d.Description, &d.Status,
			&d.ScanTrigger, &d.TripID); err != nil {
			return nil, err
		}
		out = append(out, d)
	}
	return out, rows.Err()
}

// DTCsForTrip lists fault codes recorded during one trip.
func (s *Store) DTCsForTrip(ctx context.Context, id string) ([]DTC, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT timestamp, code, description, status, scan_trigger, trip_id
		FROM dtc_events WHERE trip_id=$1 ORDER BY timestamp DESC`, id)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []DTC
	for rows.Next() {
		var d DTC
		if err := rows.Scan(&d.Timestamp, &d.Code, &d.Description, &d.Status,
			&d.ScanTrigger, &d.TripID); err != nil {
			return nil, err
		}
		out = append(out, d)
	}
	return out, rows.Err()
}

// ---- Health verdict + freshness ------------------------------------------

// Verdict is the at-a-glance health summary, derived from the same thresholds as
// the Grafana alert rules so the dashboards, alerts, and this page agree.
type Verdict struct {
	Level   string // "green" | "amber" | "red"
	Reasons []string
}

// Health computes the verdict over recent windows (data lands ~one drive late, so
// the windows match the alert rules: 3 days for temps/charging/DTC, 7 for battery).
func (s *Store) Health(ctx context.Context) (Verdict, error) {
	var (
		maxCoolant, maxTrans, maxBattery, p10Battery *float64
		p05Oil, maxKnock, misfireRate                *float64
		dtcCount                                      int64
	)
	_ = s.pool.QueryRow(ctx, `SELECT max(coolant_temp_c) FROM obd_5s WHERE timestamp > now() - interval '3 days'`).Scan(&maxCoolant)
	_ = s.pool.QueryRow(ctx, `SELECT max(trans_temp_c) FROM ford_obd_5s WHERE timestamp > now() - interval '3 days'`).Scan(&maxTrans)
	_ = s.pool.QueryRow(ctx, `SELECT max(battery_v) FROM obd_30s WHERE timestamp > now() - interval '3 days'`).Scan(&maxBattery)
	_ = s.pool.QueryRow(ctx, `SELECT percentile_cont(0.10) WITHIN GROUP (ORDER BY battery_v) FROM obd_30s WHERE timestamp > now() - interval '7 days' AND battery_v IS NOT NULL`).Scan(&p10Battery)
	// p05 (not min) of oil pressure ignores the odd startup/idle blip — only a
	// sustained drop pulls the 5th percentile below the 200 kPa floor.
	_ = s.pool.QueryRow(ctx, `SELECT percentile_cont(0.05) WITHIN GROUP (ORDER BY oil_pressure_kpa) FROM ford_obd_10s WHERE timestamp > now() - interval '3 days' AND oil_pressure_kpa IS NOT NULL`).Scan(&p05Oil)
	_ = s.pool.QueryRow(ctx, `SELECT max(knock_retard_deg) FROM ford_obd_10s WHERE timestamp > now() - interval '3 days'`).Scan(&maxKnock)
	// Aggregate misfire rate over the window: sum each drive's misfires and
	// combustion events separately, then divide — keeps numerator and denominator
	// over the same drives (a window-max count over total-window events would lie).
	_ = s.pool.QueryRow(ctx, `
		WITH per AS (
			SELECT t.id,
				(SELECT sum(rpm)/30.0 FROM obd_1s o WHERE o.trip_id=t.id AND o.rpm IS NOT NULL) AS events,
				(SELECT COALESCE(max(misfire_acc_cyl1),0)+COALESCE(max(misfire_acc_cyl2),0)+COALESCE(max(misfire_acc_cyl3),0)+COALESCE(max(misfire_acc_cyl4),0) FROM ford_obd_20s m WHERE m.trip_id=t.id) AS mis
			FROM trips t WHERE t.start_time > now() - interval '3 days')
		SELECT sum(mis) / NULLIF(sum(events), 0) * 100 FROM per`).Scan(&misfireRate)
	if err := s.pool.QueryRow(ctx, `SELECT count(*) FROM dtc_events WHERE timestamp > now() - interval '3 days'`).Scan(&dtcCount); err != nil {
		return Verdict{}, err
	}

	v := Verdict{Level: "green"}
	red := func(msg string) { v.Level = "red"; v.Reasons = append(v.Reasons, msg) }
	amber := func(msg string) {
		if v.Level != "red" {
			v.Level = "amber"
		}
		v.Reasons = append(v.Reasons, msg)
	}

	if maxCoolant != nil && *maxCoolant > 110 {
		red("Coolant exceeded 110°C")
	}
	if maxTrans != nil && *maxTrans > 120 {
		red("Transmission exceeded 120°C")
	}
	if maxBattery != nil && *maxBattery < 13.0 {
		red("Battery never reached charging voltage (alternator?)")
	}
	if dtcCount > 0 {
		red("Diagnostic trouble code(s) present")
	}
	if p05Oil != nil && *p05Oil < 200 {
		red("Oil pressure running low")
	}
	if p10Battery != nil && *p10Battery < 12.0 {
		amber("Battery voltage running low")
	}
	if maxKnock != nil && *maxKnock > 6 {
		amber("Engine pulling timing (knock)")
	}
	if misfireRate != nil && *misfireRate > 0.5 {
		amber("Misfire rate elevated")
	}
	if len(v.Reasons) == 0 {
		v.Reasons = append(v.Reasons, "All monitored systems within normal range")
	}
	return v, nil
}

// LoggerHealth is the latest snapshot of the in-car Pi's own health — is the data
// pipeline itself alive and will the USB drive last to 300k.
type LoggerHealth struct {
	At             time.Time
	CPUTempC       *float64
	DiskFreeMb     *float64
	MemFreeMb      *float64
	UptimeS        *int64
	USBMounted     *int
	BTPresent      *int
	ReconnectCount *int
	RestartCount   *int
	RTCOk          *int
	LastError      *string
}

// LoggerHealth returns the most recent pi_health_log row, or ok=false if none.
func (s *Store) LoggerHealth(ctx context.Context) (LoggerHealth, bool, error) {
	var h LoggerHealth
	err := s.pool.QueryRow(ctx, `
		SELECT timestamp, cpu_temp_c, disk_free_mb, memory_free_mb, uptime_s,
		       usb_drive_mounted, bt_adapter_present, obd_reconnect_count,
		       restart_count, rtc_ok, last_error
		FROM pi_health_log ORDER BY timestamp DESC LIMIT 1`).
		Scan(&h.At, &h.CPUTempC, &h.DiskFreeMb, &h.MemFreeMb, &h.UptimeS,
			&h.USBMounted, &h.BTPresent, &h.ReconnectCount, &h.RestartCount,
			&h.RTCOk, &h.LastError)
	if err != nil {
		if isNoRows(err) {
			return LoggerHealth{}, false, nil
		}
		return LoggerHealth{}, false, err
	}
	return h, true, nil
}

// Cadence is the recent driving rate used to project time-to-300k.
type Cadence struct {
	KmPerDay float64
	Days     float64 // span of data the rate is based on
}

// Cadence computes the average logged km/day over the available trip history
// (capped to the most recent 90 days so the projection reflects current usage).
func (s *Store) Cadence(ctx context.Context) (Cadence, bool, error) {
	var c Cadence
	err := s.pool.QueryRow(ctx, `
		WITH recent AS (
			SELECT distance_km, start_time FROM trip_summary
			WHERE start_time > now() - interval '90 days'
		)
		SELECT COALESCE(sum(distance_km), 0),
		       GREATEST(EXTRACT(EPOCH FROM (max(start_time) - min(start_time))) / 86400.0, 0)
		FROM recent`).Scan(&c.KmPerDay, &c.Days)
	if err != nil {
		return Cadence{}, false, err
	}
	totalKm := c.KmPerDay // currently holds sum(distance_km)
	if c.Days < 1 {
		return Cadence{}, false, nil // not enough history to project
	}
	c.KmPerDay = totalKm / c.Days
	if c.KmPerDay <= 0 {
		return Cadence{}, false, nil
	}
	return c, true, nil
}

// LastSync returns the most recent pi_health_log timestamp (proxy for "last time
// the car synced"), or ok=false if there is none.
func (s *Store) LastSync(ctx context.Context) (time.Time, bool, error) {
	var ts *time.Time
	if err := s.pool.QueryRow(ctx, `SELECT max(timestamp) FROM pi_health_log`).Scan(&ts); err != nil {
		return time.Time{}, false, err
	}
	if ts == nil {
		return time.Time{}, false, nil
	}
	return *ts, true, nil
}

// ---- Sparkline series -----------------------------------------------------

func (s *Store) floatSeries(ctx context.Context, sql string, args ...any) ([]float64, error) {
	rows, err := s.pool.Query(ctx, sql, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []float64
	for rows.Next() {
		var v float64
		if err := rows.Scan(&v); err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, rows.Err()
}

// Overview "last N days" trends — one point per drive, oldest → newest.

func (s *Store) TrendCoolantPeak(ctx context.Context, days int) ([]float64, error) {
	return s.floatSeries(ctx, `SELECT max(o.coolant_temp_c) FROM trips t JOIN obd_5s o ON o.trip_id=t.id
		WHERE t.start_time > now() - make_interval(days => $1)
		GROUP BY t.id, t.start_time HAVING max(o.coolant_temp_c) IS NOT NULL ORDER BY t.start_time`, days)
}

func (s *Store) TrendTransPeak(ctx context.Context, days int) ([]float64, error) {
	return s.floatSeries(ctx, `SELECT max(o.trans_temp_c) FROM trips t JOIN ford_obd_5s o ON o.trip_id=t.id
		WHERE t.start_time > now() - make_interval(days => $1)
		GROUP BY t.id, t.start_time HAVING max(o.trans_temp_c) IS NOT NULL ORDER BY t.start_time`, days)
}

func (s *Store) TrendBatteryMin(ctx context.Context, days int) ([]float64, error) {
	return s.floatSeries(ctx, `SELECT min(o.battery_v) FROM trips t JOIN obd_30s o ON o.trip_id=t.id
		WHERE t.start_time > now() - make_interval(days => $1)
		GROUP BY t.id, t.start_time HAVING min(o.battery_v) IS NOT NULL ORDER BY t.start_time`, days)
}

func (s *Store) TrendBatteryMax(ctx context.Context, days int) ([]float64, error) {
	return s.floatSeries(ctx, `SELECT max(o.battery_v) FROM trips t JOIN obd_30s o ON o.trip_id=t.id
		WHERE t.start_time > now() - make_interval(days => $1)
		GROUP BY t.id, t.start_time HAVING max(o.battery_v) IS NOT NULL ORDER BY t.start_time`, days)
}

func (s *Store) TrendMaxSpeed(ctx context.Context, days int) ([]float64, error) {
	return s.floatSeries(ctx, `SELECT max(o.speed_kmh)::float8 FROM trips t JOIN obd_1s o ON o.trip_id=t.id
		WHERE t.start_time > now() - make_interval(days => $1)
		GROUP BY t.id, t.start_time HAVING max(o.speed_kmh) IS NOT NULL ORDER BY t.start_time`, days)
}

func (s *Store) TrendRpmPeak(ctx context.Context, days int) ([]float64, error) {
	return s.floatSeries(ctx, `SELECT max(o.rpm)::float8 FROM trips t JOIN obd_1s o ON o.trip_id=t.id
		WHERE t.start_time > now() - make_interval(days => $1)
		GROUP BY t.id, t.start_time HAVING max(o.rpm) IS NOT NULL ORDER BY t.start_time`, days)
}

func (s *Store) TrendIntakePeak(ctx context.Context, days int) ([]float64, error) {
	return s.floatSeries(ctx, `SELECT max(o.intake_air_temp_c) FROM trips t JOIN obd_5s o ON o.trip_id=t.id
		WHERE t.start_time > now() - make_interval(days => $1)
		GROUP BY t.id, t.start_time HAVING max(o.intake_air_temp_c) IS NOT NULL ORDER BY t.start_time`, days)
}

func (s *Store) TrendAmbientAvg(ctx context.Context, days int) ([]float64, error) {
	return s.floatSeries(ctx, `SELECT avg(o.ambient_air_temp_c) FROM trips t JOIN obd_30s o ON o.trip_id=t.id
		WHERE t.start_time > now() - make_interval(days => $1)
		GROUP BY t.id, t.start_time HAVING avg(o.ambient_air_temp_c) IS NOT NULL ORDER BY t.start_time`, days)
}

func (s *Store) TrendAvgMovingSpeed(ctx context.Context, days int) ([]float64, error) {
	return s.floatSeries(ctx, `SELECT avg_moving_speed_kmh FROM trip_summary
		WHERE start_time > now() - make_interval(days => $1) AND avg_moving_speed_kmh IS NOT NULL
		ORDER BY start_time`, days)
}

func (s *Store) TrendDistance(ctx context.Context, days int) ([]float64, error) {
	return s.floatSeries(ctx, `SELECT distance_km FROM trip_summary
		WHERE start_time > now() - make_interval(days => $1) ORDER BY start_time`, days)
}

func (s *Store) TrendOilPressureMin(ctx context.Context, days int) ([]float64, error) {
	return s.floatSeries(ctx, `SELECT min(o.oil_pressure_kpa) FROM trips t JOIN ford_obd_10s o ON o.trip_id=t.id
		WHERE t.start_time > now() - make_interval(days => $1)
		GROUP BY t.id, t.start_time HAVING min(o.oil_pressure_kpa) IS NOT NULL ORDER BY t.start_time`, days)
}

func (s *Store) TrendKnockPeak(ctx context.Context, days int) ([]float64, error) {
	return s.floatSeries(ctx, `SELECT max(o.knock_retard_deg) FROM trips t JOIN ford_obd_10s o ON o.trip_id=t.id
		WHERE t.start_time > now() - make_interval(days => $1)
		GROUP BY t.id, t.start_time HAVING max(o.knock_retard_deg) IS NOT NULL ORDER BY t.start_time`, days)
}

// TrendMisfireRate is each drive's misfire rate (%) — misfires ÷ combustion
// events — one point per drive, so drive length doesn't distort it.
func (s *Store) TrendMisfireRate(ctx context.Context, days int) ([]float64, error) {
	return s.floatSeries(ctx, `
		SELECT rate FROM (
			SELECT t.start_time,
				(SELECT COALESCE(max(misfire_acc_cyl1),0)+COALESCE(max(misfire_acc_cyl2),0)+COALESCE(max(misfire_acc_cyl3),0)+COALESCE(max(misfire_acc_cyl4),0) FROM ford_obd_20s m WHERE m.trip_id=t.id)::float8
				/ NULLIF((SELECT sum(rpm)/30.0 FROM obd_1s o WHERE o.trip_id=t.id AND o.rpm IS NOT NULL), 0) * 100 AS rate
			FROM trips t WHERE t.start_time > now() - make_interval(days => $1)
		) q WHERE rate IS NOT NULL ORDER BY start_time`, days)
}

// Per-trip curves for the trip-detail page (time-ordered samples within one trip).

func (s *Store) TripCurveCoolant(ctx context.Context, id string) ([]float64, error) {
	return s.floatSeries(ctx, `SELECT coolant_temp_c FROM obd_5s WHERE trip_id=$1 AND coolant_temp_c IS NOT NULL ORDER BY timestamp`, id)
}
func (s *Store) TripCurveTrans(ctx context.Context, id string) ([]float64, error) {
	return s.floatSeries(ctx, `SELECT trans_temp_c FROM ford_obd_5s WHERE trip_id=$1 AND trans_temp_c IS NOT NULL ORDER BY timestamp`, id)
}
func (s *Store) TripCurveSpeed(ctx context.Context, id string) ([]float64, error) {
	return s.floatSeries(ctx, `SELECT speed_kmh::float8 FROM obd_1s WHERE trip_id=$1 AND speed_kmh IS NOT NULL ORDER BY timestamp`, id)
}
func (s *Store) TripCurveRPM(ctx context.Context, id string) ([]float64, error) {
	return s.floatSeries(ctx, `SELECT rpm::float8 FROM obd_1s WHERE trip_id=$1 AND rpm IS NOT NULL ORDER BY timestamp`, id)
}
func (s *Store) TripCurveBattery(ctx context.Context, id string) ([]float64, error) {
	return s.floatSeries(ctx, `SELECT battery_v FROM obd_30s WHERE trip_id=$1 AND battery_v IS NOT NULL ORDER BY timestamp`, id)
}
func (s *Store) TripCurveOilPressure(ctx context.Context, id string) ([]float64, error) {
	return s.floatSeries(ctx, `SELECT oil_pressure_kpa FROM ford_obd_10s WHERE trip_id=$1 AND oil_pressure_kpa IS NOT NULL ORDER BY timestamp`, id)
}
func (s *Store) TripCurveKnockRetard(ctx context.Context, id string) ([]float64, error) {
	return s.floatSeries(ctx, `SELECT knock_retard_deg FROM ford_obd_10s WHERE trip_id=$1 AND knock_retard_deg IS NOT NULL ORDER BY timestamp`, id)
}

// TripCurveBoost returns the desired and actual boost curves as two aligned,
// time-ordered series (the gap between them is the turbo/wastegate health signal).
func (s *Store) TripCurveBoost(ctx context.Context, id string) (desired, actual []float64, err error) {
	rows, err := s.pool.Query(ctx, `
		SELECT COALESCE(boost_desired_psi, 0), COALESCE(boost_actual_psi, 0)
		FROM ford_obd_10s
		WHERE trip_id=$1 AND (boost_desired_psi IS NOT NULL OR boost_actual_psi IS NOT NULL)
		ORDER BY timestamp`, id)
	if err != nil {
		return nil, nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var d, a float64
		if err := rows.Scan(&d, &a); err != nil {
			return nil, nil, err
		}
		desired = append(desired, d)
		actual = append(actual, a)
	}
	return desired, actual, rows.Err()
}

func isNoRows(err error) bool {
	return err != nil && err.Error() == "no rows in result set"
}
