# Project 300K — Frontend (Part 3, Phase A)

The human-facing **car-health app**: a server-rendered (Go + [templ](https://templ.guide) +
[htmx](https://htmx.org)) web UI that reads the synced OBD data from PostgreSQL and presents
the curated, mobile-first view. It runs on the private tailnet alongside the API and Grafana.

**Division of labour:** Grafana is the *engineering/telemetry tool* (deep time-series dives,
alert rules). This app is the *at-a-glance health view* — overview, trip stories, fault log —
and the future home for service records + AI analysis (Phases B/C).

## Why server-rendered (no SPA, no Node)
One language (Go renders the HTML), no build toolchain, a ~50 KB htmx and a hand-rolled
stylesheet `go:embed`'d into the binary. The server assembles each page from live data and
sends finished HTML — so nothing is exposed and there is no client-side data layer. It ships
as one static binary in a distroless image, in the same Docker stack as everything else.

## Pages (Phase A)
| Route | Shows |
|-------|-------|
| `/` | Overview — 300k progress, health verdict (🟢/🟡/🔴 from the alert thresholds), odometer, last trip, sync freshness |
| `/trips` | Trip history (newest first) |
| `/trips/{id}` | Trip detail — stats, per-trip peaks (coolant/oil/trans/battery), fault codes |
| `/dtc` | Full diagnostic-trouble-code log |
| `/healthz` | liveness (DB ping) |

The health verdict uses the **same thresholds as the Grafana alert rules**, so dashboards,
alerts, and this page always agree.

## Layout
```
cmd/web/           entrypoint: config → pgxpool → serve
internal/config/   env loader (DATABASE_URL required; LISTEN_ADDR/WEB_PASSWORD/DEMO_MODE/ODOMETER_BASELINE_KM optional)
internal/db/       read-only pgxpool
internal/queries/  every read query → plain structs (uses the backend's views)
internal/web/      routes, basic-auth, handlers, embedded assets (htmx, css, manifest)
internal/web/views/  templ components (*.templ → *_templ.go via `templ generate`)
demo/              seed_demo.sql — synthetic data for a public demo instance
```

## Configuration
| Var | Required | Notes |
|-----|----------|-------|
| `DATABASE_URL` | yes | pgx DSN; the app only reads |
| `LISTEN_ADDR` | no | default `0.0.0.0:8090` |
| `WEB_PASSWORD` | no | basic-auth (user `owner`) on top of the tailnet; empty = no auth |
| `DEMO_MODE` | no | `true` shows a "DEMO DATA" banner (public showcase) |
| `ODOMETER_BASELINE_KM` | no | dash reading on day one; added to logged distance for the true odometer |

## Run with Docker
It's the `web` service in `backend/compose.yaml` (shares the Tailscale netns, reaches the DB on
loopback, served on the tailnet at `:8090`):
```bash
cd ../backend
make up            # builds + starts the whole stack incl. web
# open http://127.0.0.1:8090  (or http://<tailnet-ip>:8090 from your phone)
```
Installable as a phone home-screen app (PWA manifest).

## Develop locally
```bash
go run github.com/a-h/templ/cmd/templ@v0.3.1020 generate   # regenerate *_templ.go after editing .templ
go vet ./... && go build ./...
DATABASE_URL=postgres://localhost:5432/project300k_dev?sslmode=disable go run ./cmd/web
```

## Public demo (later)
Deploy a second instance with `DEMO_MODE=true` against a throwaway DB seeded with
`demo/seed_demo.sql` (synthetic trips/health/DTCs) — a clickable demo that never touches real
vehicle data. See the showcase plan.

## Roadmap
- **Phase A** (this) — overview, trips, fault log.
- **Phase B** — service records: photo upload → Claude vision extraction → review → logbook.
- **Phase C** — AI analysis: weekly "what drifted" reports, ask-about-my-car.
