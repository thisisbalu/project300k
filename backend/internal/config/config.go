// Package config loads and validates the backend server configuration from the
// environment. It mirrors the philosophy of the Pi collector's config.py: read
// from the environment, fail fast and loudly on missing required values, and
// apply sensible defaults for everything optional.
//
// Required values (the server refuses to start without them):
//
//	DATABASE_URL — PostgreSQL connection string (pgx-compatible DSN or URL)
//	API_KEY      — Bearer token the Pi must present on every /sync request.
//	               This MUST equal the API_KEY in the Pi's /etc/obd-collector/config.env.
//
// Optional values (defaults shown):
//
//	LISTEN_ADDR — 127.0.0.1:8080  (bind the server's Tailscale IP in production;
//	              the Pi reaches it over Tailscale only, never the open internet)
package config

import (
	"fmt"
	"os"
	"strings"
)

// Config is the validated server configuration. All fields are read once at
// startup and never mutated.
type Config struct {
	DatabaseURL string
	APIKey      string
	ListenAddr  string
}

// Load reads configuration from the environment, applies defaults, and validates
// required values. It returns an error listing every problem at once so the
// operator sees all of them in a single run rather than one at a time.
func Load() (*Config, error) {
	cfg := &Config{
		DatabaseURL: os.Getenv("DATABASE_URL"),
		APIKey:      os.Getenv("API_KEY"),
		ListenAddr:  envOr("LISTEN_ADDR", "127.0.0.1:8080"),
	}

	var missing []string
	if cfg.DatabaseURL == "" {
		missing = append(missing, "DATABASE_URL")
	}
	if cfg.APIKey == "" {
		missing = append(missing, "API_KEY")
	}
	if len(missing) > 0 {
		return nil, fmt.Errorf("missing required config: %s", strings.Join(missing, ", "))
	}

	return cfg, nil
}

// String renders the config for logging with the API key masked — the bearer
// token must never reach the logs.
func (c *Config) String() string {
	return fmt.Sprintf("DATABASE_URL=%s LISTEN_ADDR=%s API_KEY=***", maskDSN(c.DatabaseURL), c.ListenAddr)
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

// maskDSN hides any password embedded in a URL-style DSN before logging.
func maskDSN(dsn string) string {
	at := strings.LastIndex(dsn, "@")
	scheme := strings.Index(dsn, "://")
	if at == -1 || scheme == -1 || scheme+3 >= at {
		return dsn
	}
	creds := dsn[scheme+3 : at]
	if i := strings.Index(creds, ":"); i != -1 {
		return dsn[:scheme+3] + creds[:i] + ":***" + dsn[at:]
	}
	return dsn
}
