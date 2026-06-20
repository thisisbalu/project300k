// Package web wires the HTTP routes and renders the templ views from query data.
package web

import (
	"context"
	"embed"
	"log/slog"
	"net/http"
	"time"

	"github.com/a-h/templ"

	"project300k/frontend/internal/config"
	"project300k/frontend/internal/queries"
	"project300k/frontend/internal/web/views"
)

//go:embed assets/*
var assetsFS embed.FS

// Server holds the shared handler dependencies.
type Server struct {
	q   *queries.Store
	cfg *config.Config
	log *slog.Logger
}

func New(q *queries.Store, cfg *config.Config, log *slog.Logger) *Server {
	return &Server{q: q, cfg: cfg, log: log}
}

// Handler builds the router (Go 1.22+ patterns) wrapped in basic auth, with the
// static assets served unauthenticated so the login prompt can style itself.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.Handle("GET /static/", http.StripPrefix("/static/", http.FileServerFS(mustSub(assetsFS))))
	mux.HandleFunc("GET /healthz", s.handleHealth)

	app := http.NewServeMux()
	app.HandleFunc("GET /{$}", s.handleOverview)
	app.HandleFunc("GET /trips", s.handleTrips)
	app.HandleFunc("GET /trips/{id}", s.handleTripDetail)
	app.HandleFunc("GET /dtc", s.handleDTC)
	mux.Handle("/", basicAuth(s.cfg.Password, app))
	return mux
}

func (s *Server) handleOverview(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	d := views.PageData{BaselineKm: s.cfg.OdometerBaseKm, Demo: s.cfg.DemoMode}
	var err error
	if d.Lifetime, err = s.q.Lifetime(ctx); err != nil {
		s.fail(w, "lifetime", err)
		return
	}
	if d.Verdict, err = s.q.Health(ctx); err != nil {
		s.fail(w, "health", err)
		return
	}
	if d.LatestTrip, d.HasTrip, err = s.q.LatestTrip(ctx); err != nil {
		s.fail(w, "latest trip", err)
		return
	}
	if d.LastSync, d.HasSync, err = s.q.LastSync(ctx); err != nil {
		s.fail(w, "last sync", err)
		return
	}
	if d.Logger, d.HasLogger, err = s.q.LoggerHealth(ctx); err != nil {
		s.fail(w, "logger health", err)
		return
	}
	if d.Cadence, d.HasCadence, err = s.q.Cadence(ctx); err != nil {
		s.fail(w, "cadence", err)
		return
	}
	d.Trends = s.overviewTrends(ctx)
	s.render(w, r, views.Overview(d))
}

// overviewTrends builds the 30-day, one-point-per-drive sparklines. A failing
// series is logged and simply omitted — never fatal to the page.
func (s *Server) overviewTrends(ctx context.Context) []views.Trend {
	const days = 30
	type spec struct {
		label, unit, color string
		thr                float64
		bars               bool
		stat               string // badge value: "" (last drive) | "max" | "min"
		fn                 func(context.Context, int) ([]float64, error)
	}
	// Health cards (coolant/transmission, with red-flag thresholds) badge the
	// 30-day worst case so the headline agrees with the verdict + threshold line;
	// descriptive cards (speed/distance/RPM) badge the most recent drive.
	specs := []spec{
		{"Coolant peak / drive", "°C", "#d22f2f", 110, false, "max", s.q.TrendCoolantPeak},
		{"Transmission peak / drive", "°C", "#c77700", 120, false, "max", s.q.TrendTransPeak},
		{"Battery low / drive", "V", "#2563eb", 0, false, "min", s.q.TrendBatteryMin},
		{"Battery high / drive", "V", "#0d9488", 0, false, "", s.q.TrendBatteryMax},
		{"Max speed / drive", "km/h", "#1ca54c", 0, false, "", s.q.TrendMaxSpeed},
		{"Avg moving speed / drive", "km/h", "#1ca54c", 0, false, "", s.q.TrendAvgMovingSpeed},
		{"Engine RPM peak / drive", "rpm", "#e0529c", 0, false, "", s.q.TrendRpmPeak},
		{"Intake air peak / drive", "°C", "#7a5af5", 0, false, "", s.q.TrendIntakePeak},
		{"Ambient temp / drive", "°C", "#0d9488", 0, false, "", s.q.TrendAmbientAvg},
		{"Distance / drive", "km", "#2563eb", 0, true, "", s.q.TrendDistance},
	}
	var out []views.Trend
	for _, sp := range specs {
		vals, err := sp.fn(ctx, days)
		if err != nil {
			s.log.Warn("trend query failed", "label", sp.label, "err", err)
			continue
		}
		out = append(out, views.Trend{
			Label: sp.label, Unit: sp.unit, Values: vals,
			Color: sp.color, Threshold: sp.thr, Bars: sp.bars, Stat: sp.stat,
		})
	}
	return out
}

func (s *Server) handleTrips(w http.ResponseWriter, r *http.Request) {
	trips, err := s.q.Trips(r.Context(), 200)
	if err != nil {
		s.fail(w, "trips", err)
		return
	}
	s.render(w, r, views.Trips(trips, s.cfg.DemoMode))
}

func (s *Server) handleTripDetail(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	id := r.PathValue("id")
	trip, ok, err := s.q.TripByID(ctx, id)
	if err != nil {
		s.fail(w, "trip", err)
		return
	}
	if !ok {
		http.NotFound(w, r)
		return
	}
	peaks, err := s.q.TripPeaks(ctx, id)
	if err != nil {
		s.fail(w, "trip peaks", err)
		return
	}
	dtcs, err := s.q.DTCsForTrip(ctx, id)
	if err != nil {
		s.fail(w, "trip dtcs", err)
		return
	}
	s.render(w, r, views.TripDetail(trip, peaks, dtcs, s.tripCurves(ctx, id), s.cfg.DemoMode))
}

// tripCurves builds the per-trip sparklines (over the drive). A failing series
// is logged and omitted.
func (s *Server) tripCurves(ctx context.Context, id string) []views.Trend {
	type spec struct {
		label, unit, color string
		thr                float64
		fn                 func(context.Context, string) ([]float64, error)
	}
	specs := []spec{
		{"Coolant", "°C", "#d22f2f", 110, s.q.TripCurveCoolant},
		{"Transmission", "°C", "#c77700", 120, s.q.TripCurveTrans},
		{"Speed", "km/h", "#1ca54c", 0, s.q.TripCurveSpeed},
		{"Engine RPM", "rpm", "#e0529c", 0, s.q.TripCurveRPM},
		{"Battery", "V", "#2563eb", 0, s.q.TripCurveBattery},
	}
	var out []views.Trend
	for _, sp := range specs {
		vals, err := sp.fn(ctx, id)
		if err != nil {
			s.log.Warn("trip curve failed", "label", sp.label, "err", err)
			continue
		}
		// Badge shows the drive's peak, not the end-of-drive sample.
		out = append(out, views.Trend{Label: sp.label, Unit: sp.unit, Values: vals, Color: sp.color, Threshold: sp.thr, Stat: "max"})
	}
	return out
}

func (s *Server) handleDTC(w http.ResponseWriter, r *http.Request) {
	dtcs, err := s.q.DTCs(r.Context(), 300)
	if err != nil {
		s.fail(w, "dtc", err)
		return
	}
	s.render(w, r, views.DTCLog(dtcs, s.cfg.DemoMode))
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := timeoutCtx(r, 2*time.Second)
	defer cancel()
	if _, err := s.q.Lifetime(ctx); err != nil {
		http.Error(w, "db unreachable", http.StatusServiceUnavailable)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write([]byte(`{"status":"ok"}`))
}

func (s *Server) render(w http.ResponseWriter, r *http.Request, c templ.Component) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	if err := c.Render(r.Context(), w); err != nil {
		s.log.Error("render failed", "err", err)
	}
}

func (s *Server) fail(w http.ResponseWriter, what string, err error) {
	s.log.Error("query failed", "what", what, "err", err)
	http.Error(w, "internal error", http.StatusInternalServerError)
}
