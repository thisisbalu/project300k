// Package store turns a validated batch of /sync rows into a single multi-row
// INSERT against PostgreSQL, applying the per-table ON CONFLICT policy.
package store

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

// ErrUnknownTable is returned when a table is not in the allowlist.
var ErrUnknownTable = errors.New("unknown table")

// Row is one decoded JSON object: column name → value (string, float64, nil…).
type Row = map[string]any

// InsertRows writes every row of one table in a single multi-row INSERT and
// returns the number of rows actually inserted (DO NOTHING duplicates and, for
// trips, rows that only updated still count per CommandTag semantics).
//
// Columns come from the static allowlist, never from the request, so the only
// interpolated identifiers are trusted. All values are bound as parameters.
// A missing column in a row is inserted as SQL NULL — the Pi sends every column,
// but this keeps the writer robust to partial rows.
func InsertRows(ctx context.Context, pool *pgxpool.Pool, table string, rows []Row) (int64, error) {
	sql, args, err := buildInsert(table, rows)
	if err != nil {
		return 0, err
	}
	if sql == "" { // no rows
		return 0, nil
	}
	tag, err := pool.Exec(ctx, sql, args...)
	if err != nil {
		return 0, err
	}
	return tag.RowsAffected(), nil
}

// buildInsert composes the multi-row INSERT and its bind args for one table.
// Split out from InsertRows so the SQL shape (columns, $n::cast placeholders,
// conflict clause, missing-key→nil binding) is unit-testable without a database.
// Returns ("", nil, nil) when there are no rows. Identifiers come only from the
// allowlist; every value is a bound parameter.
func buildInsert(table string, rows []Row) (string, []any, error) {
	spec, ok := Tables[table]
	if !ok {
		return "", nil, ErrUnknownTable
	}
	if len(rows) == 0 {
		return "", nil, nil
	}

	cols := spec.Columns
	names := make([]string, len(cols))
	for i, col := range cols {
		names[i] = col.Name
	}

	args := make([]any, 0, len(rows)*len(cols))
	tuples := make([]string, 0, len(rows))
	n := 0
	for _, row := range rows {
		ph := make([]string, len(cols))
		for i, col := range cols {
			n++
			ph[i] = fmt.Sprintf("$%d::%s", n, col.Cast)
			args = append(args, row[col.Name]) // missing key → nil → NULL
		}
		tuples = append(tuples, "("+strings.Join(ph, ", ")+")")
	}

	sql := fmt.Sprintf(
		"INSERT INTO %s (%s) VALUES %s %s",
		table,
		strings.Join(names, ", "),
		strings.Join(tuples, ", "),
		conflictClause(spec.Conflict),
	)
	return sql, args, nil
}

func conflictClause(p ConflictPolicy) string {
	switch p {
	case UpsertTrip:
		// trips is the one mutable row — see ConflictPolicy doc and the Pi's
		// sync.py "Idempotency / upsert contract". DO NOTHING here would swallow
		// the trip close and leave every multi-run trip permanently open.
		//
		// COALESCE so a re-delivered OPEN payload (end_time/duration_s NULL,
		// e.g. a duplicate or out-of-order retry) can never blank out a trip that
		// already closed. A trip end is monotonic — it never legitimately reverts
		// to NULL — so keeping the existing non-NULL value is strictly safe.
		return "ON CONFLICT (id) DO UPDATE SET " +
			"end_time = COALESCE(EXCLUDED.end_time, trips.end_time), " +
			"duration_s = COALESCE(EXCLUDED.duration_s, trips.duration_s)"
	default:
		return "ON CONFLICT (id) DO NOTHING"
	}
}

// IsForeignKeyViolation reports whether err is a PostgreSQL FK violation (23503).
// The handler maps this to a non-2xx so the Pi keeps the orphaned child batch
// unsynced and retries it on the next run, once the parent trip has landed.
func IsForeignKeyViolation(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.Code == "23503"
}
