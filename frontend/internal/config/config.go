// Package config loads the frontend server configuration from the environment,
// mirroring the backend's fail-fast philosophy: required values are validated at
// startup, everything optional has a sane default.
package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
)

// Config is the validated frontend configuration, read once at startup.
type Config struct {
	DatabaseURL    string  // pgx DSN (read-only use); required
	ListenAddr     string  // default 0.0.0.0:8090
	Password       string  // optional basic-auth password (user "owner"); empty = no auth
	DemoMode       bool    // show a DEMO DATA banner (public showcase instance)
	OdometerBaseKm float64 // dash odometer when logging began; added to logged distance
}

// Load reads, defaults, and validates the configuration.
func Load() (*Config, error) {
	cfg := &Config{
		DatabaseURL:    os.Getenv("DATABASE_URL"),
		ListenAddr:     envOr("LISTEN_ADDR", "0.0.0.0:8090"),
		Password:       os.Getenv("WEB_PASSWORD"),
		DemoMode:       strings.EqualFold(os.Getenv("DEMO_MODE"), "true") || os.Getenv("DEMO_MODE") == "1",
		OdometerBaseKm: envFloat("ODOMETER_BASELINE_KM", 0),
	}
	if cfg.DatabaseURL == "" {
		return nil, fmt.Errorf("missing required config: DATABASE_URL")
	}
	return cfg, nil
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envFloat(key string, def float64) float64 {
	if v := os.Getenv(key); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil {
			return f
		}
	}
	return def
}
