// Package api wires the HTTP routes that receive synced data from the Pi.
//
// Routes:
//
//	POST /sync/{table}  — authenticated; inserts a batch of rows (the Pi's contract)
//	GET  /healthz       — unauthenticated DB ping
package api

import (
	"encoding/json"
	"log/slog"
	"net/http"

	"github.com/jackc/pgx/v5/pgxpool"
)

// Server holds the dependencies the handlers share.
type Server struct {
	pool   *pgxpool.Pool
	apiKey string
	log    *slog.Logger
}

// New builds a Server.
func New(pool *pgxpool.Pool, apiKey string, log *slog.Logger) *Server {
	return &Server{pool: pool, apiKey: apiKey, log: log}
}

// Handler returns the router. Go 1.22+ method+wildcard patterns mean no
// third-party router is needed; the {table} segment is read via PathValue.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("POST /sync/{table}", s.requireBearer(s.handleSync))
	return mux
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}
