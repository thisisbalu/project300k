# Migrating the backend to the real home server

The laptop is the **temporary** home server. When the mini-PC arrives, the whole
stack lifts and shifts: copy the folder, set a server `.env`, restore the data.
The only thing that genuinely *changes* is the backup destination — iCloud has no
headless Linux client, so the off-box copy switches to rclone (or a LAN copy).

Nothing here is destructive on the laptop. Keep the laptop stack running until the
server is fully verified, then retire it.

Estimated time: **20–30 minutes.**

---

## 1. Prep the server (one-time)

```bash
# Docker + compose plugin (Debian/Ubuntu)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"     # log out/in afterwards
```

## 2. Copy the code

```bash
git clone <your-repo> project300k && cd project300k/backend
# (or scp the backend/ folder across)
```

`.env` is gitignored, so it does **not** come with the clone — recreate it in step 3.

## 3. Create the server's own `.env`

Start from `.env.example`. Most values carry over; these **must change**:

| Var | What to do |
|-----|------------|
| `TS_AUTHKEY` | Generate a **new** key (Tailscale console → Settings → Keys). Each node needs its own; the laptop's key is single-use/already-claimed. |
| `TS_HOSTNAME` | New name, e.g. `project300k-server`. |
| `API_KEY` | **Keep identical to the Pi's** value (reuse the laptop's) — otherwise you must also update the Pi in step 5. |
| `DB_PASSWORD` | Generate fresh: `openssl rand -hex 16`. |
| `GF_ADMIN_PASSWORD` | Generate fresh: `openssl rand -hex 16`. |
| `BACKUP_DIR` | Change to a **local Linux path**, e.g. `/srv/project300k-backups` (no iCloud on Linux). |
| `TZ` | The server's timezone. |

## 4. Carry the data over

On the **laptop**, force a fresh dump and grab the newest file:

```bash
make backup-now
# newest file in your iCloud project300k-backups/daily/
```

Copy it to the server, then restore into a fresh DB:

```bash
# on the SERVER
mkdir -p /srv/project300k-backups          # = BACKUP_DIR
docker compose up -d db                     # start just the DB (empty, self-migrates)
cat project300k_XXXXXXXX.dump | docker compose exec -T db \
  pg_restore -U app -d project300k --clean --if-exists --no-owner
docker compose up -d                        # bring up the rest of the stack
```

> The api applies migrations on startup, so the empty DB already has the schema before
> restore; `--clean --if-exists` makes the restore idempotent if you run it twice.

## 5. Point the Pi at the new server

```bash
make ts-ip          # on the server → its tailnet IP
```

Edit the Pi's `/etc/obd-collector/config.env`:

```
API_URL=http://<server-tailnet-ip>:8080/sync
TAILSCALE_IP=<server-tailnet-ip>
API_KEY=<unchanged if you reused it in step 3>
```

Then `jarvis restart`. The Pi syncs to the new box on its next drive.

## 6. Switch backups to off-box (the one real change)

iCloud doesn't run headless on Linux, so the off-box half of the backup changes.
The **dump job in `scripts/backup.sh` stays identical** — only where `/backups` is
shipped changes. Pick one:

### Option A — cloud via rclone (off-site; recommended)

`rclone` speaks ~70 backends. Good choices:

- **Backblaze B2** — first **10 GB free** (dumps are ~2 MB each; rotation keeps ~25 MB
  total, so effectively free forever at this scale). Built for backups, simplest setup.
- **Google Drive** — fine if you already have Google storage; fussier OAuth.
- Also S3 / Wasabi / Dropbox / OneDrive, etc.
- **Not iCloud** — rclone has no real iCloud Drive backend (the reason this step exists).

```bash
sudo apt install rclone          # or: curl https://rclone.org/install.sh | sudo bash
rclone config                    # one-time: create remote, e.g. name it "b2"
```

Then append a push to the end of a successful dump in `scripts/backup.sh` (a ~1-line
change after the `prune` calls):

```sh
rclone copy "$OUT" b2:project300k-backups --quiet || log "WARN: rclone push failed"
```

rclone runs on the host, not in the postgres container — either install it on the host
and run the push from a host cron/systemd timer pointed at `BACKUP_DIR`, or add an
rclone-capable sidecar. (Decide at migration time; both are small.)

### Option B — LAN copy (on-site; zero cost, zero account)

If the mini-PC is at home, the off-box copy can just be another machine:

```bash
# push dumps to the laptop / a NAS over SSH (host cron or appended to backup.sh)
rsync -a /srv/project300k-backups/ user@nas:/backups/project300k/
```

No account, no cloud — but on-site only (won't survive fire/theft). Backblaze B2 is
worth the 10 minutes if off-site matters to you.

## 7. Verify, then retire the laptop

```bash
curl -s http://<server-tailnet-ip>:8080/healthz     # api up
make grafana-url                                     # open Grafana on the new IP
make backup-ls                                       # dumps exist locally
# confirm a drive syncs end-to-end, and dumps reach the cloud/LAN remote
```

Only **after** the server is proven (a real drive synced, Grafana shows it, a backup
landed off-box) — shut down the laptop stack (`make down` on the laptop).

---

## Quick reference — what changes vs. what carries over

| | Laptop (now) | Linux server (later) |
|---|---|---|
| Code (`backend/`) | — | same, git clone / scp |
| `compose.yaml`, `scripts/`, `grafana/` | — | **unchanged** |
| `API_KEY` | set | **keep same** (else update Pi) |
| `TS_AUTHKEY` / `TS_HOSTNAME` | laptop's | **new per node** |
| `DB_PASSWORD` / `GF_ADMIN_PASSWORD` | laptop's | fresh |
| Data | live in `pgdata` volume | **restored from a dump** |
| `BACKUP_DIR` | iCloud Drive folder | local dir (`/srv/...`) |
| Off-box backup | iCloud auto-sync | **rclone → B2** (or LAN copy) |
| Pi `config.env` | laptop IP | **server IP** |
