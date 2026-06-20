// Package views holds the templ components (server-rendered HTML) and the small
// formatting helpers they use.
package views

import (
	"fmt"
	"time"

	"project300k/frontend/internal/queries"
)

// PageData bundles everything the overview page needs.
type PageData struct {
	Lifetime   queries.Lifetime
	Verdict    queries.Verdict
	LatestTrip queries.Trip
	HasTrip    bool
	BaselineKm float64
	LastSync   time.Time
	HasSync    bool
	Demo       bool
	Trends     []Trend
	Logger     queries.LoggerHealth
	HasLogger  bool
	Cadence    queries.Cadence
	HasCadence bool
}

// OdometerKm is the true odometer estimate: dash baseline + logged distance.
func (d PageData) OdometerKm() float64 { return d.BaselineKm + d.Lifetime.TotalKm }

// ProgressPct is progress toward 300,000 km, clamped to [0,100].
func (d PageData) ProgressPct() float64 {
	p := d.OdometerKm() / 300000.0 * 100.0
	if p < 0 {
		return 0
	}
	if p > 100 {
		return 100
	}
	return p
}

// KmPerMonth is the recent driving rate, for the projection line.
func (d PageData) KmPerMonth() float64 { return d.Cadence.KmPerDay * 30.0 }

// YearsTo300k projects, at the recent rate, how long until 300,000 km.
func (d PageData) YearsTo300k() float64 {
	remaining := 300000.0 - d.OdometerKm()
	if remaining <= 0 || d.Cadence.KmPerDay <= 0 {
		return 0
	}
	return remaining / (d.Cadence.KmPerDay * 365.0)
}

// ETA300k is the projected calendar date of crossing 300,000 km.
func (d PageData) ETA300k() time.Time {
	return time.Now().AddDate(0, 0, int(d.YearsTo300k()*365.0+0.5))
}

func (d PageData) ProjectionText() string {
	y := d.YearsTo300k()
	switch {
	case y <= 0:
		return "Arrived"
	case y < 1:
		return fmt.Sprintf("~%d months to go", int(y*12+0.5))
	default:
		return fmt.Sprintf("~%.1f years to go", y)
	}
}

func km(v float64) string    { return grouped(int64(v+0.5)) + " km" }
func km1(v float64) string   { return fmt.Sprintf("%.1f km", v) }
func pct(v float64) string   { return fmt.Sprintf("%.1f%%", v) }
func degC(v float64) string  { return fmt.Sprintf("%.0f°C", v) }
func volts(v float64) string { return fmt.Sprintf("%.2f V", v) }
func kmh(v int) string       { return fmt.Sprintf("%d km/h", v) }
func kmhF(v float64) string  { return fmt.Sprintf("%.0f km/h", v) }

// grouped renders an integer with thousands separators (Go's fmt has none).
func grouped(n int64) string {
	s := fmt.Sprintf("%d", n)
	neg := false
	if len(s) > 0 && s[0] == '-' {
		neg, s = true, s[1:]
	}
	var out []byte
	for i, c := range []byte(s) {
		if i > 0 && (len(s)-i)%3 == 0 {
			out = append(out, ',')
		}
		out = append(out, c)
	}
	if neg {
		return "-" + string(out)
	}
	return string(out)
}

// dur renders a duration in seconds as "1h 23m" / "5m 12s".
func dur(seconds int) string {
	d := time.Duration(seconds) * time.Second
	h := int(d.Hours())
	m := int(d.Minutes()) % 60
	s := int(d.Seconds()) % 60
	switch {
	case h > 0:
		return fmt.Sprintf("%dh %dm", h, m)
	case m > 0:
		return fmt.Sprintf("%dm %ds", m, s)
	default:
		return fmt.Sprintf("%ds", s)
	}
}

func ts(t time.Time) string  { return t.Local().Format("2006-01-02 15:04") }
func tsd(t time.Time) string { return t.Local().Format("Mon 2 Jan, 15:04") }

// ago renders a coarse "x ago" for freshness.
func ago(t time.Time) string {
	d := time.Since(t)
	switch {
	case d < time.Minute:
		return "just now"
	case d < time.Hour:
		return fmt.Sprintf("%dm ago", int(d.Minutes()))
	case d < 24*time.Hour:
		return fmt.Sprintf("%dh ago", int(d.Hours()))
	default:
		return fmt.Sprintf("%dd ago", int(d.Hours())/24)
	}
}

// optInt / optFloat render nullable values with a dash fallback.
func optDeg(v *float64) string {
	if v == nil {
		return "—"
	}
	return degC(*v)
}
func optVolts(v *float64) string {
	if v == nil {
		return "—"
	}
	return volts(*v)
}
func optKmh(v *int) string {
	if v == nil {
		return "—"
	}
	return kmh(*v)
}
func optKmhF(v *float64) string {
	if v == nil {
		return "—"
	}
	return kmhF(*v)
}
func optDur(v *int) string {
	if v == nil {
		return "—"
	}
	return dur(*v)
}
func optKpa(v *float64) string {
	if v == nil {
		return "—"
	}
	return fmt.Sprintf("%.0f kPa", *v)
}
func optPsi(v *float64) string {
	if v == nil {
		return "—"
	}
	return fmt.Sprintf("%.1f psi", *v)
}
func optDegRet(v *float64) string {
	if v == nil {
		return "—"
	}
	return fmt.Sprintf("%.1f°", *v)
}

// disk renders a free-space figure (stored in MB) as MB/GB.
func disk(v *float64) string {
	if v == nil {
		return "—"
	}
	if *v >= 1024 {
		return fmt.Sprintf("%.1f GB", *v/1024)
	}
	return fmt.Sprintf("%.0f MB", *v)
}
func optTempC(v *float64) string {
	if v == nil {
		return "—"
	}
	return degC(*v)
}
func optCount(v *int) string {
	if v == nil {
		return "—"
	}
	return fmt.Sprintf("%d", *v)
}

// lowDisk flags when the USB free space (MB) is getting tight (< 1 GB).
func lowDisk(v *float64) bool { return v != nil && *v < 1024 }

// flagGood reports whether a 0/1 health flag is in its good (1) state. A nil
// reading is treated as good so we don't cry wolf on missing data.
func flagGood(v *int) bool { return v == nil || *v == 1 }

// flag renders a 0/1 health flag as a word; flagOK reports whether it's the good state.
func flag(v *int, good, bad string) string {
	if v == nil {
		return "—"
	}
	if *v == 1 {
		return good
	}
	return bad
}

// uptimeShort renders seconds as "3d 4h" / "5h 12m" / "8m".
func uptimeShort(v *int64) string {
	if v == nil {
		return "—"
	}
	d := time.Duration(*v) * time.Second
	days := int(d.Hours()) / 24
	h := int(d.Hours()) % 24
	m := int(d.Minutes()) % 60
	switch {
	case days > 0:
		return fmt.Sprintf("%dd %dh", days, h)
	case h > 0:
		return fmt.Sprintf("%dh %dm", h, m)
	default:
		return fmt.Sprintf("%dm", m)
	}
}

func deref[T any](p *T, def T) T {
	if p == nil {
		return def
	}
	return *p
}
func str(p *string, def string) string {
	if p == nil || *p == "" {
		return def
	}
	return *p
}
