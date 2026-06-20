package store

import (
	"strings"
	"testing"
)

// Every allowlisted table must lead with id (the conflict target) and only
// trips may use the upsert policy — the rest are insert-once.
func TestTablesAllowlistInvariants(t *testing.T) {
	for name, spec := range Tables {
		if len(spec.Columns) == 0 {
			t.Errorf("%s: no columns", name)
			continue
		}
		if spec.Columns[0].Name != "id" {
			t.Errorf("%s: first column is %q, want id", name, spec.Columns[0].Name)
		}
		if name == "trips" {
			if spec.Conflict != UpsertTrip {
				t.Errorf("trips: conflict policy = %v, want UpsertTrip", spec.Conflict)
			}
		} else if spec.Conflict != DoNothing {
			t.Errorf("%s: conflict policy = %v, want DoNothing", name, spec.Conflict)
		}
	}
}

func TestBuildInsertNoRows(t *testing.T) {
	sql, args, err := buildInsert("obd_1s", nil)
	if err != nil || sql != "" || args != nil {
		t.Fatalf("got (%q, %v, %v), want empty", sql, args, err)
	}
}

func TestBuildInsertUnknownTable(t *testing.T) {
	if _, _, err := buildInsert("robots", []Row{{"id": "x"}}); err != ErrUnknownTable {
		t.Fatalf("err = %v, want ErrUnknownTable", err)
	}
}

func TestBuildInsertShapeAndMissingKeys(t *testing.T) {
	rows := []Row{
		{"id": "u1", "trip_id": "t1", "timestamp": "2026-06-17T12:00:00+00:00",
			"rpm": 820.0, "speed_kmh": 0.0, "throttle_pct": 14.1, "load_pct": 22.0,
			"synced": 0.0}, // unknown column — must be dropped
		{"id": "u2", "trip_id": "t1", "timestamp": "2026-06-17T12:00:01+00:00",
			"rpm": 900.0}, // missing speed/throttle/load — must bind as nil
	}
	sql, args, err := buildInsert("obd_1s", rows)
	if err != nil {
		t.Fatal(err)
	}

	// 7 columns × 2 rows = 14 bind args; synced is not among them.
	if len(args) != 14 {
		t.Fatalf("args len = %d, want 14", len(args))
	}
	// Column list is the allowlist order, no synced.
	if !strings.Contains(sql, "(id, trip_id, timestamp, rpm, speed_kmh, throttle_pct, load_pct)") {
		t.Errorf("unexpected column list in: %s", sql)
	}
	if strings.Contains(sql, "synced") {
		t.Errorf("synced leaked into SQL: %s", sql)
	}
	// Casts and conflict clause present.
	if !strings.Contains(sql, "$1::uuid") || !strings.Contains(sql, "::timestamptz") {
		t.Errorf("missing type casts in: %s", sql)
	}
	if !strings.Contains(sql, "ON CONFLICT (id) DO NOTHING") {
		t.Errorf("missing conflict clause in: %s", sql)
	}
	// Row 1 synced value dropped; row 2 missing columns bound as nil.
	// args layout: [id,trip_id,ts,rpm,speed,throttle,load] × 2
	if args[3] != 820.0 {
		t.Errorf("row1 rpm = %v, want 820", args[3])
	}
	if args[11] != nil || args[12] != nil || args[13] != nil {
		t.Errorf("row2 missing speed/throttle/load should be nil, got %v %v %v",
			args[11], args[12], args[13])
	}
}

func TestConflictClauseTrips(t *testing.T) {
	got := conflictClause(UpsertTrip)
	for _, want := range []string{
		"DO UPDATE",
		"end_time = COALESCE(EXCLUDED.end_time, trips.end_time)",
		"duration_s = COALESCE(EXCLUDED.duration_s, trips.duration_s)",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("trips conflict clause missing %q: %s", want, got)
		}
	}
}
