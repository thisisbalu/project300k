// Package migrations embeds the numbered .sql migration files so they ship
// inside the server binary — no separate migration files to deploy.
package migrations

import "embed"

// FS holds every migrations/NNNN_*.sql file. The db migration runner applies
// them in filename order.
//
//go:embed *.sql
var FS embed.FS
