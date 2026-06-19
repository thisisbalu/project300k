"""
sync.py — Batch sync of unsynced SQLite rows to the home server API.

Runs once per drive. Started at boot by obd-sync.service (Type=simple) and
loops: it waits for connectivity (the Pi stays in NetworkManager autoconnect
mode, so it associates with the hotspot whenever it appears at any point in the
drive), then drains the entire backlog. On the first fully successful drain it
exits; the unit's ExecStopPost runs `nmcli device disconnect wlan0`, which drops
the link and suppresses autoconnect until the next reboot — so the Pi syncs once
per drive, stays off afterwards, and reconnects proactively on the next boot.
This means a drive's data lands at the start of the next drive (one-drive lag).

Sync flow:
    1. Network check — confirm hotspot is up (wlan0 has IP) and server is
       reachable (ping Tailscale IP). Logs a distinct message for each failure
       mode so the cause is immediately clear when reading logs via SSH.

    2. Pi health snapshot — collect metrics and count unsynced rows, write
       to pi_health_log, then include in the sync payload so the server
       always receives an up-to-date health record.

    3. Per-table batch sync in priority order — most valuable data first.
       For each table: read SYNC_BATCH_SIZE rows WHERE synced=0, POST to the
       API, mark synced=1 on HTTP 200. Repeat until no unsynced rows remain.
       On failure, log the error and move to the next table — a partial sync
       is better than no sync.

    4. Summary log — total rows synced across all tables.

Sync order:
    trips FIRST — it is the parent table; every data row carries
    trip_id REFERENCES trips(id). Children must reach the server after their
    parent trip exists or the server's foreign key rejects them. Among the
    children, obd_1s comes first (highest frequency, most valuable for trends).

Idempotency / upsert contract (server side):
    Each row has a UUID primary key. All tables EXCEPT trips are insert-once
    and the server must use:
        ON CONFLICT (id) DO NOTHING
    so retrying a partially-synced batch silently ignores already-stored rows.

    trips is the one MUTABLE row: it is first synced open (end_time NULL) so
    children have a parent to reference, then re-synced closed after
    update_trip_end() resets synced=0 with the final end_time/duration_s.
    The server MUST therefore upsert trips with:
        ON CONFLICT (id) DO UPDATE
            SET end_time = EXCLUDED.end_time,
                duration_s = EXCLUDED.duration_s
    A DO NOTHING on trips would swallow the close and leave every multi-run
    trip permanently open on the server.
"""

from __future__ import annotations

import subprocess
import sqlite3
import time
import uuid
from datetime import datetime, timezone

import requests

import health
from config import config
from logger import configure_sync_logging, logger
from storage import get_connection

# Maximum batches per table per sync run — guards against an infinite loop
# if the UPDATE synced=1 silently fails and rows never leave the queue.
_MAX_BATCHES_PER_TABLE = 1000

# Retry attempts for the post-POST UPDATE synced=1 on OperationalError.
_SYNCED_UPDATE_RETRIES = 5

# trips first — parent of every trip_id FK. Children follow, obd_1s highest
# priority among them. See the module docstring's "Sync order" note.
SYNC_TABLE_ORDER = [
    "trips",
    "obd_1s",
    "obd_5s",
    "obd_30s",
    "ford_obd_5s",
    "ford_obd_10s",
    "ford_obd_20s",
    "dtc_events",
    "pi_health_log",
]


def run() -> None:
    """Proactive once-per-drive sync loop — entry point for obd-sync.service.

    The Pi stays in NetworkManager autoconnect mode, so it associates with the
    hotspot whenever it is available at any point during the drive. This loop
    waits for connectivity, then drains the whole backlog. On the first fully
    successful drain it returns (exit 0); the unit's ExecStopPost then runs
    `nmcli device disconnect wlan0`, dropping the link and suppressing autoconnect
    until the next reboot — so the Pi syncs once per drive and stays off after,
    reconnecting proactively next boot.

    If connectivity never appears, the loop keeps polling cheaply until the car
    powers the Pi off (the power cycle bounds it). A pass that fails partway
    (server drops mid-drain) is retried after SYNC_POLL_S.
    """
    # Sync runs as a separate process — log to stderr→journald only, never the
    # collector's USB file (RotatingFileHandler is not multi-process safe).
    configure_sync_logging()
    logger.info("Sync started — waiting for hotspot/connectivity (one sync per drive)")

    while True:
        if _check_network():
            if _sync_pass():
                logger.info("Sync complete for this drive — releasing the link")
                return
            logger.warning(f"Sync pass incomplete — retrying in {config.SYNC_POLL_S}s")
        time.sleep(config.SYNC_POLL_S)


def _sync_pass() -> bool:
    """Run one full drain pass over all tables; return True only if every table
    fully drained (nothing left to retry this drive).

    Opens a fresh connection per pass with verify_integrity=False — the collector
    owns integrity management at boot, so a full-DB scan every pass is wasted IO.
    """
    conn = get_connection(verify_integrity=False)
    try:
        _write_health_snapshot(conn)
        total_rows = 0
        pass_ok = True
        for table in SYNC_TABLE_ORDER:
            synced, ok = _sync_table(conn, table)
            total_rows += synced
            pass_ok = pass_ok and ok
        logger.info(
            f"Sync pass: {total_rows} rows across {len(SYNC_TABLE_ORDER)} tables "
            f"({'complete' if pass_ok else 'incomplete'})"
        )
        return pass_ok
    finally:
        conn.close()


def _check_network() -> bool:
    """Verify the hotspot is connected and the server is reachable.

    Step 1: Check wlan0 has an IP address — confirms iPhone hotspot is connected.
    Step 2: Ping TAILSCALE_IP — confirms server is reachable over VPN.

    Logs a distinct message for each failure so the cause is unambiguous
    when reading the log file via SSH.

    Returns:
        True if both checks pass, False otherwise.
    """
    # Check wlan0 has an IP — indicates iPhone hotspot is connected.
    try:
        result = subprocess.run(
            ["ip", "addr", "show", "wlan0"],
            capture_output=True, text=True, timeout=5
        )
        if "inet " not in result.stdout:
            # DEBUG, not INFO: run() polls this every SYNC_POLL_S while waiting
            # for the hotspot, so an INFO line here would spam the journal.
            logger.debug("No hotspot yet (wlan0 has no IP)")
            return False
    except FileNotFoundError:
        logger.warning("Network check failed (wlan0): 'ip' binary not found — check PATH")
        return False
    except Exception as e:
        logger.warning(f"Network check failed (wlan0): {e}")
        return False

    # Ping Tailscale IP — confirms server is reachable over VPN.
    # -c 1: one packet, -W 3: 3s timeout, -q: quiet output.
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "3", "-q", config.TAILSCALE_IP],
            capture_output=True, timeout=5
        )
        if result.returncode != 0:
            # DEBUG, not INFO — polled every SYNC_POLL_S; see note above.
            logger.debug(f"Server unreachable (ping {config.TAILSCALE_IP} failed)")
            return False
    except Exception as e:
        logger.warning(f"Network check failed (ping): {e}")
        return False

    return True


def _write_health_snapshot(conn: sqlite3.Connection) -> None:
    """Collect Pi health metrics and write to pi_health_log.

    Counts total unsynced rows across all tables before sync so the
    server can see how much data accumulated since the last sync.

    obd_reconnect_count is read from the file written by OBDConnection —
    the collector and sync script run as separate processes, so the count
    cannot be read from memory directly.
    """
    # Count total unsynced rows across all data tables.
    rows_pending = 0
    for table in SYNC_TABLE_ORDER:
        try:
            cursor = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE synced=0")
            rows_pending += cursor.fetchone()[0]
        except sqlite3.OperationalError:
            # Table does not exist yet — Ford tables are added after FORScan
            # confirms Mode 22 addresses. Skip silently at DEBUG level so it
            # is visible in logs without being noisy in normal operation.
            logger.debug(f"Table '{table}' does not exist yet — skipping count")

    metrics = health.collect(
        obd_reconnect_count=health.read_reconnect_count(),
        restart_count=health.read_restart_count(),
        rtc_ok=health.read_rtc_ok(),
    )
    metrics["rows_collected"] = rows_pending

    row = {
        "id": str(uuid.uuid4()),
        # Column name is "timestamp" in pi_health_log schema — not "synced_at".
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "synced": 0,
        **metrics,
    }

    try:
        columns = ", ".join(row.keys())
        placeholders = ", ".join(f":{k}" for k in row.keys())
        conn.execute(f"INSERT INTO pi_health_log ({columns}) VALUES ({placeholders})", row)
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Failed to write health snapshot: {e}")


def _sync_table(conn: sqlite3.Connection, table: str) -> tuple[int, bool]:
    """Sync all unsynced rows for one table to the server in batches.

    Reads SYNC_BATCH_SIZE rows WHERE synced=0, serialises to JSON,
    POSTs to the API with Bearer auth. On HTTP 200, marks rows synced=1
    with a retry loop for OperationalError (database locked by collector).
    Repeats until no unsynced rows remain or _MAX_BATCHES_PER_TABLE is hit.

    On POST failure, logs the error and returns the count synced so far —
    the caller retries the rest later this drive.

    Args:
        conn:  Active SQLite connection.
        table: Table name to sync.

    Returns:
        (rows_synced, ok). ok is False when the table did not fully drain this
        pass — a POST failed, rows could not be marked synced, or the per-table
        batch cap was hit — so the caller (run loop) knows to retry. A table that
        does not exist yet (Ford tables pre-FORScan) is a clean skip: ok=True.
    """
    total_synced = 0
    batches = 0
    ok = True

    while batches < _MAX_BATCHES_PER_TABLE:
        batches += 1

        try:
            cursor = conn.execute(
                f"SELECT * FROM {table} WHERE synced=0 LIMIT ?",
                (config.SYNC_BATCH_SIZE,)
            )
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            # Table does not exist yet — Ford tables added after FORScan scan.
            logger.debug(f"Table '{table}' does not exist yet — skipping sync")
            break

        if not rows:
            break

        payload = [dict(row) for row in rows]
        row_ids = [r["id"] for r in payload]

        try:
            response = requests.post(
                f"{config.API_URL}/{table}",
                json={"table": table, "rows": payload},
                headers={"Authorization": f"Bearer {config.API_KEY}"},
                timeout=30,
            )
            response.raise_for_status()

            # Mark successfully synced rows — use a single UPDATE with IN clause
            # for efficiency rather than one UPDATE per row.
            # Retry on OperationalError: the collector holds a write lock during
            # batch commits. If the UPDATE fails, the rows were already POSTed
            # successfully — the server's ON CONFLICT (id) DO NOTHING makes
            # a retry safe, but we must mark them locally to stop re-sending.
            placeholders = ",".join("?" * len(row_ids))
            update_sql = f"UPDATE {table} SET synced=1 WHERE id IN ({placeholders})"
            marked = False
            for attempt in range(_SYNCED_UPDATE_RETRIES):
                try:
                    conn.execute(update_sql, row_ids)
                    conn.commit()
                    marked = True
                    break
                except sqlite3.OperationalError as lock_err:
                    if attempt < _SYNCED_UPDATE_RETRIES - 1:
                        time.sleep(0.2 * (attempt + 1))
                    else:
                        logger.error(
                            f"Failed to mark {len(row_ids)} rows synced in '{table}' "
                            f"after {_SYNCED_UPDATE_RETRIES} attempts: {lock_err}"
                        )

            # If the local UPDATE never landed the rows are still synced=0, so the
            # next SELECT would return the identical batch and re-POST it. Stop now
            # and let the next sync run retry once the writer releases the lock —
            # the server's ON CONFLICT(id) DO NOTHING makes the re-POST harmless.
            if not marked:
                ok = False
                break

            total_synced += len(row_ids)
            logger.info(f"Synced {len(row_ids)} rows from {table} (total: {total_synced})")

        except requests.RequestException as e:
            logger.error(f"Sync POST failed for {table}: {e} — will retry this drive")
            ok = False
            break

    if batches >= _MAX_BATCHES_PER_TABLE:
        ok = False
        logger.warning(
            f"Reached max batch limit ({_MAX_BATCHES_PER_TABLE}) for '{table}' — "
            "possible stuck sync loop. Remaining rows deferred to the next pass."
        )

    return total_synced, ok


if __name__ == "__main__":  # pragma: no cover
    run()
