package views

import (
	"fmt"
	"strings"
)

// Server-rendered SVG sparklines — tiny glanceable trend lines, no JS, no deps.
// viewBox is fixed; preserveAspectRatio="none" lets the SVG stretch to its CSS
// box, and vector-effect:non-scaling-stroke (in app.css) keeps the line crisp.

const (
	sparkW = 140.0
	sparkH = 36.0
	sparkP = 3.0
)

// Trend is one labelled series rendered as a sparkline.
type Trend struct {
	Label     string
	Unit      string // "°C" | "V" | "km/h" | "km" | "rpm"
	Values    []float64
	Color     string
	Threshold float64 // >0 draws a dashed red-flag reference line
	Bars      bool    // render as mini bars instead of a line
	Stat      string  // badge value: "" / "last" (default), "max", "min"

	// Optional second overlaid series (e.g. boost desired vs actual). When set,
	// both lines share one scale and a small legend names them.
	Values2 []float64
	Color2  string
	LegendA string // name for the primary series (Values)
	LegendB string // name for the secondary series (Values2)
}

func (t Trend) Has() bool  { return len(t.Values) > 0 }
func (t Trend) Has2() bool { return len(t.Values2) > 0 }

// LastLabel renders the badge value. Default is the last point (overview: the most
// recent drive). Per-trip curves use "max" because the last sample is end-of-drive
// (speed/RPM = 0/idle at shutdown), which is meaningless as a summary.
func (t Trend) LastLabel() string {
	if len(t.Values) == 0 {
		return "—"
	}
	v := t.Values[len(t.Values)-1]
	switch t.Stat {
	case "max":
		_, v = minMax(t.Values)
	case "min":
		v, _ = minMax(t.Values)
	}
	switch t.Unit {
	case "°C":
		return degC(v)
	case "V":
		return volts(v)
	case "km/h":
		return kmhF(v)
	case "km":
		return km1(v)
	case "rpm":
		return fmt.Sprintf("%.0f rpm", v)
	case "kPa":
		return fmt.Sprintf("%.0f kPa", v)
	case "psi":
		return fmt.Sprintf("%.1f psi", v)
	case "°":
		return fmt.Sprintf("%.1f°", v)
	case "%mf": // misfire rate — tiny values, 3 decimals
		if v == 0 {
			return "0%"
		}
		return fmt.Sprintf("%.3f%%", v)
	case "%ft": // fuel trim — single decimal, may be negative
		return fmt.Sprintf("%.1f%%", v)
	default:
		return fmt.Sprintf("%.0f %s", v, t.Unit)
	}
}

// StatLabel describes which value the badge shows, so the mixed cards (some
// badge the 30-day worst case, some the most recent drive) are self-explanatory.
func (t Trend) StatLabel() string {
	switch t.Stat {
	case "max":
		return "30-day max"
	case "min":
		return "30-day low"
	default:
		return "last drive"
	}
}

// SVG renders the sparkline markup (used via @templ.Raw in the views).
func (t Trend) SVG() string {
	if t.Bars {
		return sparkBars(t.Values, t.Color)
	}
	if len(t.Values2) > 0 {
		return spark2(t.Values, t.Color, t.Values2, t.Color2)
	}
	return spark(t.Values, t.Color, t.Threshold)
}

func downsample(v []float64, max int) []float64 {
	if len(v) <= max {
		return v
	}
	out := make([]float64, 0, max)
	step := float64(len(v)) / float64(max)
	for i := 0; i < max; i++ {
		out = append(out, v[int(float64(i)*step)])
	}
	out[len(out)-1] = v[len(v)-1]
	return out
}

func minMax(v []float64) (float64, float64) {
	mn, mx := v[0], v[0]
	for _, x := range v {
		if x < mn {
			mn = x
		}
		if x > mx {
			mx = x
		}
	}
	return mn, mx
}

func spark(vals []float64, color string, threshold float64) string {
	if len(vals) == 0 {
		return emptySpark()
	}
	vals = downsample(vals, 80)
	mn, mx := minMax(vals)
	if threshold > 0 && threshold > mx {
		mx = threshold
	}
	if threshold > 0 && threshold < mn {
		mn = threshold
	}
	rng := mx - mn
	if rng == 0 {
		rng = 1
	}
	n := len(vals)
	xat := func(i int) float64 {
		if n == 1 {
			return sparkW / 2
		}
		return sparkP + float64(i)/float64(n-1)*(sparkW-2*sparkP)
	}
	yat := func(v float64) float64 {
		return sparkH - sparkP - (v-mn)/rng*(sparkH-2*sparkP)
	}
	pts := make([]string, n)
	for i, v := range vals {
		pts[i] = fmt.Sprintf("%.1f,%.1f", xat(i), yat(v))
	}
	var b strings.Builder
	fmt.Fprintf(&b, `<svg class="spark" viewBox="0 0 %g %g" preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">`, sparkW, sparkH)
	fmt.Fprintf(&b, `<polygon class="spark-fill" style="fill:%s" points="%.1f,%.1f %s %.1f,%.1f"/>`,
		color, xat(0), sparkH, strings.Join(pts, " "), xat(n-1), sparkH)
	if threshold > 0 {
		ty := yat(threshold)
		fmt.Fprintf(&b, `<line class="spark-th" x1="0" y1="%.1f" x2="%g" y2="%.1f"/>`, ty, sparkW, ty)
	}
	fmt.Fprintf(&b, `<polyline class="spark-line" style="stroke:%s" points="%s"/>`, color, strings.Join(pts, " "))
	fmt.Fprintf(&b, `<circle cx="%.1f" cy="%.1f" r="2" style="fill:%s"/>`, xat(n-1), yat(vals[n-1]), color)
	b.WriteString(`</svg>`)
	return b.String()
}

// spark2 overlays two series on one shared scale (e.g. boost desired vs actual).
func spark2(a []float64, colorA string, b []float64, colorB string) string {
	if len(a) == 0 && len(b) == 0 {
		return emptySpark()
	}
	a = downsample(a, 80)
	b = downsample(b, 80)
	mn, mx := minMax(append(append([]float64{}, a...), b...))
	rng := mx - mn
	if rng == 0 {
		rng = 1
	}
	line := func(vals []float64, color string) string {
		n := len(vals)
		if n == 0 {
			return ""
		}
		xat := func(i int) float64 {
			if n == 1 {
				return sparkW / 2
			}
			return sparkP + float64(i)/float64(n-1)*(sparkW-2*sparkP)
		}
		yat := func(v float64) float64 { return sparkH - sparkP - (v-mn)/rng*(sparkH-2*sparkP) }
		pts := make([]string, n)
		for i, v := range vals {
			pts[i] = fmt.Sprintf("%.1f,%.1f", xat(i), yat(v))
		}
		return fmt.Sprintf(`<polyline class="spark-line" style="stroke:%s" points="%s"/><circle cx="%.1f" cy="%.1f" r="2" style="fill:%s"/>`,
			color, strings.Join(pts, " "), xat(n-1), yat(vals[n-1]), color)
	}
	var sb strings.Builder
	fmt.Fprintf(&sb, `<svg class="spark" viewBox="0 0 %g %g" preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">`, sparkW, sparkH)
	sb.WriteString(line(b, colorB)) // draw the reference (desired) first, under
	sb.WriteString(line(a, colorA))
	sb.WriteString(`</svg>`)
	return sb.String()
}

func sparkBars(vals []float64, color string) string {
	if len(vals) == 0 {
		return emptySpark()
	}
	vals = downsample(vals, 48)
	_, mx := minMax(vals)
	if mx <= 0 {
		mx = 1
	}
	n := len(vals)
	const gap = 1.0
	bw := (sparkW-2*sparkP)/float64(n) - gap
	if bw < 0.5 {
		bw = 0.5
	}
	var b strings.Builder
	fmt.Fprintf(&b, `<svg class="spark" viewBox="0 0 %g %g" preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">`, sparkW, sparkH)
	for i, v := range vals {
		x := sparkP + float64(i)*(bw+gap)
		bh := v / mx * (sparkH - 2*sparkP)
		if bh < 0 {
			bh = 0
		}
		fmt.Fprintf(&b, `<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="0.5" style="fill:%s"/>`,
			x, sparkH-sparkP-bh, bw, bh, color)
	}
	b.WriteString(`</svg>`)
	return b.String()
}

func emptySpark() string {
	return `<svg class="spark" viewBox="0 0 140 36" preserveAspectRatio="none"><text x="70" y="23" text-anchor="middle" class="spark-empty">no data yet</text></svg>`
}
