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
		dtcCount                                     int64
	)
	_ = s.pool.QueryRow(ctx, `SELECT max(coolant_temp_c) FROM obd_5s WHERE timestamp > now() - interval '3 days'`).Scan(&maxCoolant)
	_ = s.pool.QueryRow(ctx, `SELECT max(trans_temp_c) FROM ford_obd_5s WHERE timestamp > now() - interval '3 days'`).Scan(&maxTrans)
	_ = s.pool.QueryRow(ctx, `SELECT max(battery_v) FROM obd_30s WHERE timestamp > now() - interval '3 days'`).Scan(&maxBattery)
	_ = s.pool.QueryRow(ctx, `SELECT percentile_cont(0.10) WITHIN GROUP (ORDER BY battery_v) FROM obd_30s WHERE timestamp > now() - interval '7 days' AND battery_v IS NOT NULL`).Scan(&p10Battery)
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
	if p10Battery != nil && *p10Battery < 12.0 {
		amber("Battery voltage running low")
	}
	if len(v.Reasons) == 0 {
		v.Reasons = append(v.Reasons, "All monitored systems within normal range")
	}
	return v, nil
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

func isNoRows(err error) bool {
	return err != nil && err.Error() == "no rows in result set"
}
