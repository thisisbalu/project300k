"""
sync.py — Batch sync of unsynced SQLite rows to the home server API.

Run as a one-shot script by the obd-sync.timer systemd timer, firing
5 minutes after boot and every 5 minutes thereafter while the Pi is
running. The 5-minute delay gives the iPhone hotspot time to connect.

Sync flow:
    1. Network check — confirm hotspot is up (wlan0 has IP) and server
       is reachable (ping Tailscale IP). Log distinct message for each
       failure mode so SSH debugging is unambiguous.

    2. Pi health snapshot — collect and write to pi_health_log before
       syncing so the server always receives an up-to-date health record.

    3. Per-table batch sync in priority order — most valuable data first.
       For each table, reads SYNC_BATCH_SIZE rows WHERE synced=0, POSTs
       to the API, marks rows synced=1 on HTTP 200. Repeats until no
       unsynced rows remain. On failure, logs the error and moves to the
       next table — a partial sync is better than no sync.

    4. Summary log — total rows synced across all tables.

Priority order rationale:
    obd_1s first — highest frequency, most valuable for trend analysis.
    trips last — trip metadata is only complete after rows are synced.

Idempotency:
    Each row has a UUID primary key. The server uses ON CONFLICT (id) DO
    NOTHING, so retrying a batch that partially succeeded is safe — already-
    synced rows are silently ignored.
"""

from logger import logger
from config import config


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
    # TODO: Task 13 — implement full sync cycle
    pass


def _check_network() -> bool:
    """Verify the hotspot is connected and the server is reachable.

    Step 1: Check wlan0 has an IP address (hotspot connected).
    Step 2: Ping TAILSCALE_IP (server reachable over VPN).

    Logs a distinct message for each failure so the cause is immediately
    clear when reading the log via SSH.

    Returns:
        True if both checks pass, False otherwise.
    """
    # TODO: Task 13 — check wlan0 IP, ping Tailscale IP
    pass


def _sync_table(conn, table: str) -> int:
    """Sync all unsynced rows for one table in batches.

    Reads SYNC_BATCH_SIZE rows WHERE synced=0, POSTs them to the API
    as a JSON array, marks rows synced=1 on HTTP 200. Repeats until
    no unsynced rows remain or a POST fails.

    Args:
        conn:  Active SQLite connection.
        table: Table name to sync (must be in SYNC_TABLE_ORDER).

    Returns:
        Total number of rows successfully synced for this table.
    """
    # TODO: Task 13 — batch read, POST, mark synced, return count
    pass


if __name__ == "__main__":
    run()
