# Project 300K — Backend (Part 2, foundation slice)

Home-server API that receives synced OBD data from the in-car Pi and writes it to
PostgreSQL, plus Grafana dashboards over the tailnet. Built so far: PostgreSQL
schema + migrations + the Go `/sync` API + tests, derived distance/odometer views,
and provisioned Grafana dashboards. Claude API analysis, alerting, and backup are
deferred to follow-up work.

## Contract (fixed by the Pi)

The Pi (`pi/src/sync.py`) defines the wire contract; the server conforms to it:

- `POST /sync/{table}` with header `Authorization: Bearer <API_KEY>`
- Body: `{"table": "<table>", "rows": [ {col: val, ...}, ... ]}` — rows are full
  `SELECT *`, so every column is present (`NULL` as JSON `null`) plus a `synced`
  field the server ignores.
- Any 2xx marks the batch synced on the Pi, so the server returns 2xx **only** when
  the whole batch is stored. `trips` upserts (`end_time`/`duration_s`); all other
  tables are insert-once (`ON CONFLICT (id) DO NOTHING`). An orphan child (parent
  trip not synced yet) returns **409** so the Pi retries next run.
- `GET /healthz` — unauthenticated DB ping.

## Layout

```
cmd/server/        entrypoint: config → pgxpool → migrate → serve
internal/config/   env loader (DATABASE_URL, API_KEY required; LISTEN_ADDR optional)
internal/db/       pgxpool + embedded migration runner
internal/api/      routes, bearer auth, /sync + /healthz handlers
internal/store/    table/column allowlist + multi-row INSERT builder
migrations/        NNNN_*.sql (embedded into the binary)
grafana/           provisioned datasource + dashboards (mounted into the container)
```

Schema mirrors the Pi's SQLite schema (`pi/src/storage.py`) with two deliberate
deviations: no `synced` column (Pi-local bookkeeping), and Postgres-native types
(`uuid`, `timestamptz`, `integer`, `double precision`).

`0002_views.sql` adds domain views (`engine`, `thermals`, `fueling`, `boost`,
`electrical`, `transmission`, `misfires`) that Grafana reads. `0003_trip_distance.sql`
derives distance the vehicle never reports: this Bronco exposes no odometer PID, so
the Pi leaves `distance_km` NULL and `trip_summary` integrates `speed_kmh` over
`obd_1s` (trapezoidal, 5 s gap cap so a BT reconnect can't add phantom km).
`lifetime_distance` is the running total since logging began — the 300k progress
number. Both are plain views (no refresh to orchestrate).

## Configuration

Copy `.env.example` → `.env` and set values (the `Makefile` auto-loads `.env`):

| Var | Required | Notes |
|-----|----------|-------|
| `DATABASE_URL` | yes | pgx URL, e.g. `postgres://localhost:5432/project300k_dev?sslmode=disable` |
| `API_KEY` | yes | **Must equal** the Pi's `config.env` `API_KEY` |
| `LISTEN_ADDR` | no | default `127.0.0.1:8080`; bind the Tailscale IP in production |

## Run locally

```bash
make createdb          # createdb project300k_dev
cp .env.example .env    # set API_KEY
make run                # applies migrations on startup, listens on LISTEN_ADDR
```

Simulate a Pi sync (trips first, then a child):

```bash
TID=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee
curl -fsS -X POST localhost:8080/sync/trips -H "Authorization: Bearer $API_KEY" \
  -d "{\"table\":\"trips\",\"rows\":[{\"id\":\"$TID\",\"trip_number\":1,\"start_time\":\"2026-06-17T12:00:00+00:00\",\"end_time\":null,\"synced\":0}]}"
curl -fsS -X POST localhost:8080/sync/obd_1s -H "Authorization: Bearer $API_KEY" \
  -d "{\"table\":\"obd_1s\",\"rows\":[{\"id\":\"f0000000-0000-0000-0000-000000000001\",\"trip_id\":\"$TID\",\"timestamp\":\"2026-06-17T12:00:01+00:00\",\"rpm\":820,\"synced\":0}]}"
psql project300k_dev -c "select * from engine;"
```

## Run with Docker (tailscale + db + api)

The whole stack runs as three containers tied together by `compose.yaml`:

- **tailscale** — a sidecar that joins your private tailnet (nothing installs on the
  host). It owns the network namespace; `db` and `api` share it.
- **api** — listens `0.0.0.0:8080`, so it's reachable on the laptop's **tailnet IP**
  (that's how the Pi reaches it). Also published to `127.0.0.1:8080` for local dev.
- **db** — `postgres:16`, bound to `127.0.0.1` so it is **never exposed on the tailnet**;
  the api reaches it over the shared loopback. Data lives in the `pgdata` volume on the
  host, so rebuilding any container never touches it.

No public internet exposure: the Pi reaches the laptop only over the encrypted Tailscale
mesh, with the `API_KEY` bearer token on top (both required).

```bash
cp .env.example .env     # set API_KEY, DB_PASSWORD, and TS_AUTHKEY
make up                  # build + start tailscale, db, api (api self-migrates)
make ts-status           # tailscale node up + authenticated
make ts-ip               # → this laptop's tailnet IP (100.x.y.z)
curl -s localhost:8080/healthz
make psql                # psql shell inside the db container
make logs                # follow api logs
make down                # stop (data in pgdata is preserved)
```

### Grafana dashboards

Grafana runs as a fourth container sharing the tailscale namespace, so its UI is on
the laptop's **tailnet IP at :3000** — reachable from your phone or the Pi over the
private mesh (and `127.0.0.1:3000` locally). Set `GF_ADMIN_PASSWORD` in `.env`; log
in as `admin`.

```bash
make grafana-url     # print the local + tailnet URLs
make grafana-logs    # follow grafana logs
```

The Postgres datasource and dashboards are **provisioned from `grafana/`** (checked
into git), so they rebuild identically on the real server. Dashboards (longevity-core
first): **Overview** (300k progress gauge + per-trip distance + recent trips),
**Thermals**, **Electrical** (battery/fuel), **Transmission**, **Misfires**. The
Overview's `Odometer baseline (km)` textbox is where you enter the car's real odometer
reading on day one — it's added to the logged distance for the true odometer.

One-time tailnet setup:

1. Create a Tailscale account (tailscale.com), then on the **Pi**:
   `curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`
2. In the Tailscale admin console → **Settings → Keys**, generate a reusable, non-ephemeral
   **auth key** → put it in `.env` as `TS_AUTHKEY`, then `make up`.
3. `make ts-ip` to get the laptop's tailnet IP, then edit the **Pi's**
   `/etc/obd-collector/config.env`:
   ```
   API_URL=http://<laptop-tailnet-ip>:8080/sync
   TAILSCALE_IP=<laptop-tailnet-ip>
   API_KEY=<same value as the backend .env API_KEY>
   ```
   then `jarvis restart`. The Pi syncs to the laptop once per drive (boot-triggered).

> The laptop must stay **awake, plugged in, online, with Docker running** while it serves.
> `caffeinate -s` helps. If it sleeps, syncs just pause — no data is lost; the Pi keeps its
> backlog and retries.

### Backups

A `backup` container (postgres:16, so `pg_dump` matches the server) runs a daily
dump with rotation. It's deliberately split into two halves so the server move is
trivial: the **dump job never changes**, only **where the folder syncs**.

- **Make the dump** — `pg_dump -Fc` (compressed custom format) → `BACKUP_DIR/daily/`,
  keeping `KEEP_DAILY` (7) days; Monday dumps are promoted to `weekly/`, keeping
  `KEEP_WEEKLY` (4). A row-count fingerprint **skips dumps on days nothing synced**
  (car didn't move), but forces one at least every `FORCE_AFTER_DAYS` (7).
- **Ship it off-box** — `BACKUP_DIR` is bind-mounted from the host:
  - **Now (Mac):** point `BACKUP_DIR` at an **iCloud Drive** folder → dumps sync off
    the laptop automatically. *That* is the real off-box copy.
  - **Later (Linux server):** point `BACKUP_DIR` at a local dir and add an `rclone`
    push to cloud (e.g. Backblaze B2). The dump job is byte-for-byte the same.

```bash
make backup-now     # force a dump right now (ignores the unchanged-skip)
make backup-ls      # list dumps (daily + weekly)
make backup-logs    # follow the backup container
```

**Restore** a dump into the running db (replaces current data):

```bash
DUMP="$BACKUP_DIR/daily/project300k_YYYYMMDDThhmmssZ.dump"
cat "$DUMP" | docker compose exec -T db pg_restore -U app -d project300k --clean --if-exists --no-owner
```

> iCloud may evict (dataless-stub) old dumps to save space; they re-download on
> access, so restores still work. Verified: a dump restores cleanly into a throwaway
> db with matching row counts.

### Moving to the real home server later

Same folder, same command — install Docker on the server, copy `backend/`, set its own
`.env` (incl. its own `TS_AUTHKEY`), `docker compose up -d --build`. Only the setup moves;
the server's DB starts empty. **Carry the data over** by copying the newest dump from
`BACKUP_DIR` to the server and `pg_restore`-ing it into the fresh `project300k` (see
**Backups**). On the server, switch `BACKUP_DIR` to a local dir + add an `rclone` push.

See **[MIGRATION.md](MIGRATION.md)** for the full step-by-step runbook (server prep,
`.env` changes, data restore, repointing the Pi, and backup options).

## Test

```bash
make test       # unit tests only (no database)
make test-int   # unit + integration against a throwaway local test DB
```

Integration tests run only when `TEST_DATABASE_URL` is set (`make test-int` wires it
to a freshly recreated `project300k_test`). They cover the round trip, idempotent
re-POST, the trips upsert, orphan-child rejection (and that the row is **not** stored),
and auth/unknown-table handling.

## Deferred (follow-up plans)

Claude API analysis · ntfy/email alerts · rclone push for backups (on the real
server) · server provisioning · more dashboards (Engine, Boost, Fueling, Pi health).
