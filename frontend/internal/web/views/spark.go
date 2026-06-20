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
}

func (t Trend) Has() bool { return len(t.Values) > 0 }

func (t Trend) LastLabel() string {
	if len(t.Values) == 0 {
		return "—"
	}
	v := t.Values[len(t.Values)-1]
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
	default:
		return fmt.Sprintf("%.0f %s", v, t.Unit)
	}
}

// SVG renders the sparkline markup (used via @templ.Raw in the views).
func (t Trend) SVG() string {
	if t.Bars {
		return sparkBars(t.Values, t.Color)
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
