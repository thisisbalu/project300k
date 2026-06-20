// Package web wires the HTTP routes and renders the templ views from query data.
package web

import (
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
	s.render(w, r, views.Overview(d))
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
	s.render(w, r, views.TripDetail(trip, peaks, dtcs, s.cfg.DemoMode))
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
