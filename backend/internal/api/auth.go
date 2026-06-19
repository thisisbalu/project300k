package api

import (
	"crypto/subtle"
	"net/http"
	"strings"
)

// requireBearer wraps a handler with bearer-token authentication. The token must
// equal the configured API key (the same one the Pi presents). The comparison is
// constant-time so a mismatch leaks no timing information. /healthz is left
// unauthenticated and never passes through here.
func (s *Server) requireBearer(next http.HandlerFunc) http.HandlerFunc {
	want := []byte(s.apiKey)
	return func(w http.ResponseWriter, r *http.Request) {
		got := bearerToken(r.Header.Get("Authorization"))
		if got == "" || subtle.ConstantTimeCompare([]byte(got), want) != 1 {
			writeError(w, http.StatusUnauthorized, "unauthorized")
			return
		}
		next(w, r)
	}
}

// bearerToken extracts the token from an "Authorization: Bearer <token>" header,
// or "" if the header is missing or malformed.
func bearerToken(header string) string {
	const prefix = "Bearer "
	if len(header) <= len(prefix) || !strings.EqualFold(header[:len(prefix)], prefix) {
		return ""
	}
	return strings.TrimSpace(header[len(prefix):])
}
