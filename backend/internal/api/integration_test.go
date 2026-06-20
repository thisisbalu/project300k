package api

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"

	"project300k/backend/internal/db"
)

// These tests run only when TEST_DATABASE_URL points at a disposable PostgreSQL
// database (see `make test-int`). They exercise the real handler + migrations +
// pgx against live PostgreSQL — the most valuable coverage for catching schema,
// type-cast, and conflict-policy mistakes.

const testToken = "integration-test-token"

func testServer(t *testing.T) (http.Handler, *pgxpool.Pool) {
	t.Helper()
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("TEST_DATABASE_URL not set — skipping integration tests")
	}
	ctx := context.Background()
	pool, err := db.Connect(ctx, dsn)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	t.Cleanup(pool.Close)
	if err := db.Migrate(ctx, pool); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	// Clean slate for a deterministic run.
	if _, err := pool.Exec(ctx, `TRUNCATE trips, obd_1s, obd_5s, obd_30s,
		ford_obd_5s, ford_obd_10s, ford_obd_20s, dtc_events, pi_health_log CASCADE`); err != nil {
		t.Fatalf("truncate: %v", err)
	}
	srv := New(pool, testToken, slog.New(slog.NewTextHandler(io.Discard, nil)))
	return srv.Handler(), pool
}

func post(t *testing.T, h http.Handler, table string, rows []map[string]any, token string) *httptest.ResponseRecorder {
	t.Helper()
	body, _ := json.Marshal(map[string]any{"table": table, "rows": rows})
	req := httptest.NewRequest(http.MethodPost, "/sync/"+table, bytes.NewReader(body))
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func inserted(t *testing.T, rec *httptest.ResponseRecorder) int64 {
	t.Helper()
	var out struct {
		Inserted int64 `json:"inserted"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("decode response %q: %v", rec.Body.String(), err)
	}
	return out.Inserted
}

const tripID = "11111111-1111-1111-1111-111111111111"

func openTripRow() map[string]any {
	return map[string]any{
		"id": tripID, "trip_number": 1,
		"start_time": "2026-06-17T12:00:00+00:00", "end_time": nil,
		"distance_km": nil, "duration_s": nil,
		"collector_version": "1.0.0", "synced": 0,
	}
}

func TestSyncRoundTrip(t *testing.T) {
	h, pool := testServer(t)

	// 1. Open trip — parent must land before children.
	rec := post(t, h, "trips", []map[string]any{openTripRow()}, testToken)
	if rec.Code != http.StatusOK || inserted(t, rec) != 1 {
		t.Fatalf("open trip: code=%d body=%s", rec.Code, rec.Body)
	}

	// 2. Child rows (note: NULL columns + a stray synced field).
	child := map[string]any{
		"id": "22222222-2222-2222-2222-222222222222", "trip_id": tripID,
		"timestamp": "2026-06-17T12:00:01+00:00",
		"rpm":       820, "speed_kmh": 0, "throttle_pct": 14.1, "load_pct": nil,
		"synced": 0,
	}
	rec = post(t, h, "obd_1s", []map[string]any{child}, testToken)
	if rec.Code != http.StatusOK || inserted(t, rec) != 1 {
		t.Fatalf("child insert: code=%d body=%s", rec.Code, rec.Body)
	}

	// 3. Idempotent re-POST of the identical batch — ON CONFLICT DO NOTHING.
	rec = post(t, h, "obd_1s", []map[string]any{child}, testToken)
	if rec.Code != http.StatusOK || inserted(t, rec) != 0 {
		t.Fatalf("re-POST should insert 0: code=%d body=%s", rec.Code, rec.Body)
	}

	// 4. Close the trip — trips upsert must UPDATE end_time/duration_s.
	closed := openTripRow()
	closed["end_time"] = "2026-06-17T12:30:00+00:00"
	closed["duration_s"] = 1800
	rec = post(t, h, "trips", []map[string]any{closed}, testToken)
	if rec.Code != http.StatusOK {
		t.Fatalf("close trip: code=%d body=%s", rec.Code, rec.Body)
	}
	var endTime *string
	var durationS *int
	if err := pool.QueryRow(context.Background(),
		`SELECT end_time::text, duration_s FROM trips WHERE id=$1`, tripID).
		Scan(&endTime, &durationS); err != nil {
		t.Fatalf("read closed trip: %v", err)
	}
	if endTime == nil || durationS == nil || *durationS != 1800 {
		t.Fatalf("trip not closed: end_time=%v duration_s=%v", endTime, durationS)
	}

	// 5. Verify the domain view projects the child row.
	var n int
	if err := pool.QueryRow(context.Background(), `SELECT count(*) FROM engine`).Scan(&n); err != nil {
		t.Fatalf("view query: %v", err)
	}
	if n != 1 {
		t.Fatalf("engine view rows = %d, want 1", n)
	}
}

// A re-delivered OPEN trip payload (end_time NULL) must not blank out a trip that
// already closed — the COALESCE upsert guard.
func TestSyncTripCloseNotReopenedByStaleOpen(t *testing.T) {
	h, pool := testServer(t)

	// Open, then close.
	if rec := post(t, h, "trips", []map[string]any{openTripRow()}, testToken); rec.Code != http.StatusOK {
		t.Fatalf("open: code=%d body=%s", rec.Code, rec.Body)
	}
	closed := openTripRow()
	closed["end_time"] = "2026-06-17T12:30:00+00:00"
	closed["duration_s"] = 1800
	if rec := post(t, h, "trips", []map[string]any{closed}, testToken); rec.Code != http.StatusOK {
		t.Fatalf("close: code=%d body=%s", rec.Code, rec.Body)
	}

	// Re-deliver the stale OPEN payload (end_time/duration_s NULL).
	if rec := post(t, h, "trips", []map[string]any{openTripRow()}, testToken); rec.Code != http.StatusOK {
		t.Fatalf("stale re-open: code=%d body=%s", rec.Code, rec.Body)
	}

	var endTime *string
	var durationS *int
	if err := pool.QueryRow(context.Background(),
		`SELECT end_time::text, duration_s FROM trips WHERE id=$1`, tripID).
		Scan(&endTime, &durationS); err != nil {
		t.Fatalf("read trip: %v", err)
	}
	if endTime == nil || durationS == nil || *durationS != 1800 {
		t.Fatalf("trip was reopened by stale payload: end_time=%v duration_s=%v", endTime, durationS)
	}
}

func TestSyncOrphanChildRejected(t *testing.T) {
	h, pool := testServer(t)

	orphan := map[string]any{
		"id":        "33333333-3333-3333-3333-333333333333",
		"trip_id":   "99999999-9999-9999-9999-999999999999", // no such trip
		"timestamp": "2026-06-17T12:00:01+00:00", "rpm": 700,
	}
	rec := post(t, h, "obd_1s", []map[string]any{orphan}, testToken)
	if rec.Code != http.StatusConflict {
		t.Fatalf("orphan child: code=%d, want 409; body=%s", rec.Code, rec.Body)
	}
	// Critical: the row must NOT have been stored (the Pi only marks synced on 2xx).
	var n int
	if err := pool.QueryRow(context.Background(), `SELECT count(*) FROM obd_1s`).Scan(&n); err != nil {
		t.Fatal(err)
	}
	if n != 0 {
		t.Fatalf("orphan row was stored (%d rows) — data-loss risk", n)
	}
}

func TestSyncAuthAndUnknownTable(t *testing.T) {
	h, _ := testServer(t)

	// Missing token → 401.
	if rec := post(t, h, "obd_1s", []map[string]any{{"id": "x"}}, ""); rec.Code != http.StatusUnauthorized {
		t.Fatalf("no token: code=%d, want 401", rec.Code)
	}
	// Wrong token → 401.
	if rec := post(t, h, "obd_1s", []map[string]any{{"id": "x"}}, "wrong"); rec.Code != http.StatusUnauthorized {
		t.Fatalf("wrong token: code=%d, want 401", rec.Code)
	}
	// Unknown table → 404.
	if rec := post(t, h, "robots", []map[string]any{{"id": "x"}}, testToken); rec.Code != http.StatusNotFound {
		t.Fatalf("unknown table: code=%d, want 404", rec.Code)
	}
}
