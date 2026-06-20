package web

import (
	"context"
	"io/fs"
	"net/http"
	"time"
)

// mustSub roots the embedded asset FS at the assets/ directory so files are
// served at /static/<name> rather than /static/assets/<name>.
func mustSub(f fs.FS) fs.FS {
	sub, err := fs.Sub(f, "assets")
	if err != nil {
		panic(err)
	}
	return sub
}

func timeoutCtx(r *http.Request, d time.Duration) (context.Context, context.CancelFunc) {
	return context.WithTimeout(r.Context(), d)
}
