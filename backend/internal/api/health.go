package api

import (
	"context"
	"net/http"
	"time"
)

// handleHealth implements GET /healthz: a database ping behind a short timeout.
// Unauthenticated — it exposes no data, only up/down. 200 if the DB answers,
// 503 otherwise.
func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
	defer cancel()
	if err := s.pool.Ping(ctx); err != nil {
		writeError(w, http.StatusServiceUnavailable, "database unreachable")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}
