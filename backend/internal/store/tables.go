package store

// This file is the authoritative allowlist of tables the /sync endpoint accepts
// and the columns it will insert for each. It exists for two reasons:
//
//  1. Security. PostgreSQL identifiers (table and column names) cannot be bound
//     as parameters, so they are interpolated into SQL as raw text. Restricting
//     them to this static allowlist is the injection guard — a request can never
//     name a table or column that isn't listed here.
//
//  2. Correctness. The Pi POSTs full `SELECT *` rows, which include its local
//     `synced` bookkeeping column (and could include columns the server doesn't
//     model). Only columns listed here are inserted; everything else is dropped.
//
// Columns mirror migrations/0001_init.sql exactly (minus `synced`). Each column
// carries the PostgreSQL type its placeholder is cast to ($n::<cast>): the Pi
// sends ids and timestamps as JSON strings and all numbers as JSON floats, so an
// explicit cast lets PostgreSQL convert text/float8 into uuid/timestamptz/integer
// rather than rejecting a type mismatch.

// ConflictPolicy selects the ON CONFLICT clause for a table.
type ConflictPolicy int

const (
	// DoNothing — insert-once. A re-sent row (same id) is silently ignored.
	DoNothing ConflictPolicy = iota
	// UpsertTrip — trips is the one mutable row: synced first open (end_time
	// NULL) so children have a parent, then re-synced closed. Update the two
	// columns that change at trip end.
	UpsertTrip
)

// Column is an allowlisted column and the PostgreSQL type its bind placeholder
// is cast to.
type Column struct {
	Name string
	Cast string
}

// TableSpec is the insert contract for one table.
type TableSpec struct {
	Columns  []Column
	Conflict ConflictPolicy
}

func c(name, cast string) Column { return Column{Name: name, Cast: cast} }

const (
	tUUID = "uuid"
	tTS   = "timestamptz"
	tInt  = "integer"
	tBig  = "bigint"
	tF8   = "double precision"
	tText = "text"
)

// Tables is the full allowlist, keyed by the URL path segment the Pi posts to.
var Tables = map[string]TableSpec{
	"trips": {
		Conflict: UpsertTrip,
		Columns: []Column{
			c("id", tUUID), c("trip_number", tInt),
			c("start_time", tTS), c("end_time", tTS),
			c("start_odometer_km", tF8), c("end_odometer_km", tF8),
			c("distance_km", tF8), c("duration_s", tInt),
			c("collector_version", tText),
		},
	},
	"obd_1s": {
		Conflict: DoNothing,
		Columns: []Column{
			c("id", tUUID), c("trip_id", tUUID), c("timestamp", tTS),
			c("rpm", tInt), c("speed_kmh", tInt),
			c("throttle_pct", tF8), c("load_pct", tF8),
		},
	},
	"obd_5s": {
		Conflict: DoNothing,
		Columns: []Column{
			c("id", tUUID), c("trip_id", tUUID), c("timestamp", tTS),
			c("coolant_temp_c", tF8), c("oil_temp_c", tF8), c("intake_air_temp_c", tF8),
			c("maf_gs", tF8), c("map_kpa", tF8), c("baro_pressure_kpa", tF8),
			c("stft_pct", tF8), c("ltft_pct", tF8),
			c("o2_b1s1_v", tF8), c("o2_b1s2_v", tF8),
			c("timing_advance_deg", tF8), c("fuel_rail_kpa", tF8),
		},
	},
	"obd_30s": {
		Conflict: DoNothing,
		Columns: []Column{
			c("id", tUUID), c("trip_id", tUUID), c("timestamp", tTS),
			c("battery_v", tF8), c("fuel_level_pct", tF8),
			c("ambient_air_temp_c", tF8), c("distance_since_dtc_cleared_km", tF8),
		},
	},
	"ford_obd_5s": {
		Conflict: DoNothing,
		Columns: []Column{
			c("id", tUUID), c("trip_id", tUUID), c("timestamp", tTS),
			c("trans_temp_c", tF8), c("trans_oil_temp2_c", tF8),
			c("trans_line_pressure_kpa", tF8), c("trans_gear", tInt), c("tcc_ratio", tF8),
		},
	},
	"ford_obd_10s": {
		Conflict: DoNothing,
		Columns: []Column{
			c("id", tUUID), c("trip_id", tUUID), c("timestamp", tTS),
			c("oil_pressure_kpa", tF8), c("knock_retard_deg", tF8),
			c("boost_desired_psi", tF8), c("boost_actual_psi", tF8),
			c("cac_temp_c", tF8), c("wastegate_pct", tF8),
			c("vct_intake_deg", tF8), c("vct_exhaust_deg", tF8),
		},
	},
	"ford_obd_20s": {
		Conflict: DoNothing,
		Columns: []Column{
			c("id", tUUID), c("trip_id", tUUID), c("timestamp", tTS),
			c("misfire_acc_cyl1", tInt), c("misfire_acc_cyl2", tInt),
			c("misfire_acc_cyl3", tInt), c("misfire_acc_cyl4", tInt),
		},
	},
	"dtc_events": {
		Conflict: DoNothing,
		Columns: []Column{
			c("id", tUUID), c("trip_id", tUUID), c("timestamp", tTS),
			c("code", tText), c("description", tText),
			c("status", tText), c("scan_trigger", tText),
		},
	},
	"pi_health_log": {
		Conflict: DoNothing,
		Columns: []Column{
			c("id", tUUID), c("timestamp", tTS),
			c("cpu_temp_c", tF8), c("cpu_usage_pct", tF8),
			c("memory_free_mb", tF8), c("disk_free_mb", tF8),
			c("uptime_s", tBig), c("usb_drive_mounted", tInt), c("bt_adapter_present", tInt),
			c("obd_reconnect_count", tInt), c("restart_count", tInt), c("rtc_ok", tInt),
			c("last_error", tText), c("rows_collected", tInt), c("collector_version", tText),
		},
	},
}
