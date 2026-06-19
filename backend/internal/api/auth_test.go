package api

import (
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestBearerToken(t *testing.T) {
	cases := map[string]string{
		"Bearer abc123":  "abc123",
		"bearer abc123":  "abc123", // scheme is case-insensitive
		"Bearer  spaced": "spaced", // surrounding space trimmed
		"":               "",
		"Bearer":         "",
		"Basic abc":      "",
		"Bearer ":        "",
	}
	for header, want := range cases {
		if got := bearerToken(header); got != want {
			t.Errorf("bearerToken(%q) = %q, want %q", header, got, want)
		}
	}
}

// requireBearer must reject missing/wrong tokens with 401 and let the right one
// through — without touching the database.
func TestRequireBearer(t *testing.T) {
	s := &Server{apiKey: "secret-token", log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	guarded := s.requireBearer(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	})

	cases := []struct {
		name   string
		header string
		want   int
	}{
		{"valid", "Bearer secret-token", http.StatusOK},
		{"wrong", "Bearer nope", http.StatusUnauthorized},
		{"missing", "", http.StatusUnauthorized},
		{"prefix-only", "secret-token", http.StatusUnauthorized},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodPost, "/sync/obd_1s", nil)
			if tc.header != "" {
				req.Header.Set("Authorization", tc.header)
			}
			rec := httptest.NewRecorder()
			guarded(rec, req)
			if rec.Code != tc.want {
				t.Errorf("status = %d, want %d", rec.Code, tc.want)
			}
		})
	}
}
