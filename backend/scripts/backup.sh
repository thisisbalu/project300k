#!/bin/sh
# Daily pg_dump with rotation, writing to the mounted /backups directory.
#
# Portable by design: the dump job is identical on the Mac (BACKUP_DIR -> iCloud
# Drive, which syncs it off-box) and on the real Linux server later (BACKUP_DIR ->
# a local dir + an rclone push to cloud). Only the mount/destination changes, never
# this script.
#
# Connects to Postgres via the standard PG* env vars (set in compose). Dumps are
# custom-format (`pg_dump -Fc`) — compressed and restored with `pg_restore`.
#
# Usage:
#   backup.sh          loop: dump on start if stale, then daily at BACKUP_AT
#   backup.sh once     single dump and exit (used by `make backup-now`)
set -eu

OUT=/backups
DAILY="$OUT/daily"
WEEKLY="$OUT/weekly"
FP_FILE="$OUT/.last_fingerprint"

: "${KEEP_DAILY:=7}"        # daily dumps to retain
: "${KEEP_WEEKLY:=4}"       # Monday dumps promoted to weekly, retained
: "${BACKUP_AT:=03:00}"     # daily run time (container TZ; default UTC)
: "${FORCE_AFTER_DAYS:=7}"  # dump even if data is unchanged once this stale

log() { echo "[backup $(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

# A cheap signature of the data: per-table row counts in a fixed order. If it
# matches the last successful dump's, nothing was synced (car didn't move) and we
# can skip — unless the newest dump is already older than FORCE_AFTER_DAYS.
fingerprint() {
  psql -At <<'SQL'
SELECT string_agg(c::text, '/' ORDER BY n) FROM (
  SELECT 1 n, count(*) c FROM trips
  UNION ALL SELECT 2, count(*) FROM obd_1s
  UNION ALL SELECT 3, count(*) FROM obd_5s
  UNION ALL SELECT 4, count(*) FROM obd_30s
  UNION ALL SELECT 5, count(*) FROM ford_obd_5s
  UNION ALL SELECT 6, count(*) FROM ford_obd_10s
  UNION ALL SELECT 7, count(*) FROM ford_obd_20s
  UNION ALL SELECT 8, count(*) FROM dtc_events
  UNION ALL SELECT 9, count(*) FROM pi_health_log
) t;
SQL
}

newest_dump_age_days() {
  newest=$(ls -1t "$DAILY"/*.dump 2>/dev/null | head -1 || true)
  if [ -z "$newest" ]; then echo 9999; return; fi
  echo $(( ( $(date +%s) - $(stat -c %Y "$newest") ) / 86400 ))
}

prune() {
  dir=$1; keep=$2
  ls -1t "$dir"/*.dump 2>/dev/null | tail -n +$((keep + 1)) | while IFS= read -r old; do
    rm -f "$old"; log "pruned $(basename "$old")"
  done
}

dump() {
  mkdir -p "$DAILY" "$WEEKLY"

  fp=$(fingerprint 2>/dev/null || echo "")
  last=""; [ -f "$FP_FILE" ] && last=$(cat "$FP_FILE" 2>/dev/null || echo "")
  age=$(newest_dump_age_days)
  if [ -n "$fp" ] && [ "$fp" = "$last" ] && [ "$age" -lt "$FORCE_AFTER_DAYS" ]; then
    log "data unchanged (last dump ${age}d ago) — skipping"
    return 0
  fi

  ts=$(date -u +%Y%m%dT%H%M%SZ)
  f="$DAILY/project300k_${ts}.dump"
  log "dumping -> daily/$(basename "$f")"
  if pg_dump -Fc -f "$f.partial"; then
    mv "$f.partial" "$f"
    [ -n "$fp" ] && printf '%s' "$fp" > "$FP_FILE"
    log "ok ($(du -h "$f" | cut -f1 | tr -d ' '))"
    if [ "$(date -u +%u)" = "1" ]; then
      cp "$f" "$WEEKLY/$(basename "$f")"; log "promoted to weekly"
    fi
    prune "$DAILY" "$KEEP_DAILY"
    prune "$WEEKLY" "$KEEP_WEEKLY"
  else
    rm -f "$f.partial"; log "ERROR: pg_dump failed"; return 1
  fi
}

if [ "${1:-loop}" = "once" ]; then
  dump
  exit $?
fi

# On start, dump if the newest is at least a day old (covers a fresh stack or a
# laptop that was off). Then run daily at BACKUP_AT.
if [ "$(newest_dump_age_days)" -ge 1 ]; then
  dump || log "startup dump failed — will retry on schedule"
else
  log "recent dump exists — waiting for schedule"
fi

while true; do
  now=$(date +%s)
  next=$(date -d "$BACKUP_AT" +%s 2>/dev/null || echo "")
  if [ -z "$next" ]; then next=$((now + 86400)); fi
  if [ "$next" -le "$now" ]; then next=$(date -d "tomorrow $BACKUP_AT" +%s); fi
  log "next run at $(date -d "@$next" '+%Y-%m-%d %H:%M %Z' 2>/dev/null || echo "$next")"
  sleep $((next - now))
  dump || log "scheduled dump failed — will retry next cycle"
done
