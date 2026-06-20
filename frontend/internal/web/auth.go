package web

import (
	"crypto/subtle"
	"net/http"
)

// basicAuth gates the app behind a single shared password (user "owner") on top
// of the tailnet. If no password is configured, it is a no-op. Constant-time
// compare so a wrong password leaks no timing information.
func basicAuth(password string, next http.Handler) http.Handler {
	if password == "" {
		return next
	}
	want := []byte(password)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, got, ok := r.BasicAuth()
		if !ok || subtle.ConstantTimeCompare([]byte(got), want) != 1 {
			w.Header().Set("WWW-Authenticate", `Basic realm="Project 300K"`)
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		next.ServeHTTP(w, r)
	})
}
