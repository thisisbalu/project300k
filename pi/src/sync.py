"""
sync.py — Batch sync of unsynced SQLite rows to the home server API.

Run as a one-shot script by the obd-sync.timer systemd timer, firing
5 minutes after boot and every 5 minutes thereafter while the Pi is running.
The 5-minute delay gives the iPhone hotspot time to connect and Tailscale
time to establish the VPN tunnel before the first sync attempt.

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

Priority order:
    obd_1s first — highest frequency, most valuable for trend analysis.
    trips last — trip metadata references rows that should be synced first.

Idempotency:
    Each row has a UUID primary key. The server uses ON CONFLICT (id) DO
    NOTHING so retrying a partially-synced batch is safe — already-synced
    rows are silently ignored by the server.
"""

import subprocess
import sqlite3
import uuid
from datetime import datetime, timezone

import requests

import health
from config import config
from logger import logger
from storage import get_connection


SYNC_TABLE_ORDER = [
    "obd_1s",
    "obd_5s",
    "obd_30s",
    "ford_obd_5s",
    "ford_obd_10s",
    "ford_obd_20s",
    "dtc_events",
    "pi_health_log",
    "trips",
]


def run() -> None:
    """Entry point for the sync script.

    Performs the full sync cycle: network check → health snapshot →
    per-table batch sync → summary log. Safe to run repeatedly.
    """
    logger.info("Sync started")

    if not _check_network():
        return

    conn = get_connection()

    try:
        _write_health_snapshot(conn)
        total_rows = 0

        for table in SYNC_TABLE_ORDER:
            synced = _sync_table(conn, table)
            total_rows += synced

        logger.info(f"Sync complete — {total_rows} rows synced across {len(SYNC_TABLE_ORDER)} tables")

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
            logger.info("Sync skipped — no hotspot (wlan0 has no IP)")
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
            logger.info(f"Sync skipped — server unreachable (ping {config.TAILSCALE_IP} failed)")
            return False
    except Exception as e:
        logger.warning(f"Network check failed (ping): {e}")
        return False

    return True


def _write_health_snapshot(conn: sqlite3.Connection) -> None:
    """Collect Pi health metrics and write to pi_health_log.

    Counts total unsynced rows across all tables before sync so the
    server can see how much data accumulated since the last sync.
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
        obd_reconnect_count=0,  # 0 when run standalone — collector tracks this
        restart_count=0,        # already incremented by main.py on boot
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


def _sync_table(conn: sqlite3.Connection, table: str) -> int:
    """Sync all unsynced rows for one table to the server in batches.

    Reads SYNC_BATCH_SIZE rows WHERE synced=0, serialises to JSON,
    POSTs to the API with Bearer auth. On HTTP 200, marks rows synced=1
    and repeats until no unsynced rows remain. On any failure, logs the
    error and returns the count synced so far — next run retries the rest.

    Args:
        conn:  Active SQLite connection.
        table: Table name to sync.

    Returns:
        Total number of rows successfully synced for this table.
    """
    total_synced = 0

    while True:
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
            placeholders = ",".join("?" * len(row_ids))
            conn.execute(
                f"UPDATE {table} SET synced=1 WHERE id IN ({placeholders})",
                row_ids
            )
            conn.commit()
            total_synced += len(row_ids)
            logger.info(f"Synced {len(row_ids)} rows from {table} (total: {total_synced})")

        except requests.RequestException as e:
            logger.error(f"Sync POST failed for {table}: {e} — will retry next run")
            break

    return total_synced


if __name__ == "__main__":
    run()
