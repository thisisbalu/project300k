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
    # TODO: Task 13 — two-step network check, health snapshot, per-table batch sync
    pass


def _check_network() -> bool:
    # TODO: Task 13 — check wlan0 IP, ping Tailscale IP
    pass


def _sync_table(conn, table: str) -> int:
    # TODO: Task 13 — batch read unsynced rows, POST, mark synced=1; return row count
    pass


if __name__ == "__main__":
    run()
