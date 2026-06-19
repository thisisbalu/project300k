package api

import (
	"encoding/json"
	"errors"
	"net/http"

	"project300k/backend/internal/store"
)

// maxSyncBody caps the request body. A full Pi batch is ~500 rows of ~15 small
// columns — well under a megabyte. 32 MiB is a generous abuse guard.
const maxSyncBody = 32 << 20

// syncRequest is the body the Pi POSTs: {"table": "...", "rows": [ {...}, ... ]}.
// The table is also in the URL path; if both are present they must agree.
type syncRequest struct {
	Table string      `json:"table"`
	Rows  []store.Row `json:"rows"`
}

// handleSync implements POST /sync/{table}. It validates the table against the
// allowlist, inserts the batch, and returns the inserted count.
//
// Error mapping is deliberate: only a fully-stored batch returns 2xx, because
// the Pi marks rows synced=1 on any 2xx. A foreign-key violation (orphan child
// whose parent trip hasn't synced yet) returns 409 so the Pi keeps the batch and
// retries next run, once trips-first ordering has delivered the parent.
func (s *Server) handleSync(w http.ResponseWriter, r *http.Request) {
	table := r.PathValue("table")
	if _, ok := store.Tables[table]; !ok {
		writeError(w, http.StatusNotFound, "unknown table")
		return
	}

	r.Body = http.MaxBytesReader(w, r.Body, maxSyncBody)
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	var req syncRequest
	if err := dec.Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid JSON body")
		return
	}
	if req.Table != "" && req.Table != table {
		writeError(w, http.StatusBadRequest, "table in body does not match URL")
		return
	}
	if len(req.Rows) == 0 {
		writeJSON(w, http.StatusOK, map[string]int64{"inserted": 0})
		return
	}

	inserted, err := store.InsertRows(r.Context(), s.pool, table, req.Rows)
	if err != nil {
		switch {
		case errors.Is(err, store.ErrUnknownTable):
			writeError(w, http.StatusNotFound, "unknown table")
		case store.IsForeignKeyViolation(err):
			// Parent trip not synced yet — reject so the Pi retries next run.
			s.log.Warn("sync rejected: foreign key violation", "table", table)
			writeError(w, http.StatusConflict, "unknown trip_id — parent trip not synced yet")
		default:
			s.log.Error("sync insert failed", "table", table, "err", err)
			writeError(w, http.StatusInternalServerError, "insert failed")
		}
		return
	}

	s.log.Info("sync ok", "table", table, "received", len(req.Rows), "inserted", inserted)
	writeJSON(w, http.StatusOK, map[string]int64{"inserted": inserted})
}
