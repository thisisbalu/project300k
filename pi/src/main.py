import sys
import time
from config import config
from logger import logger
from storage import get_connection, init_schema
from queue_writer import QueueWriter
from obd_connection import OBDConnection
from collector import Collector
from trip import TripManager
import health


def check_rtc() -> None:
    # TODO: Task 4 — read DS3231 OSF flag via I2C, log WARNING if dead or not found
    pass


def main() -> None:
    logger.info("Starting obd-collector")
    logger.info(f"Config loaded: {config}")

    check_rtc()
    health.increment_restart_count()

    conn = get_connection()
    init_schema(conn)

    queue_writer = QueueWriter(conn)
    queue_writer.start()

    obd_connection = OBDConnection()
    obd_connection.connect()

    trip_manager = TripManager(queue_writer, obd_connection)
    collector = Collector(obd_connection, queue_writer, trip_manager)
    collector.start()

    # TODO: Task 15 — watchdog ping loop
    try:
        while True:
            time.sleep(30)
    except KeyboardInterrupt:
        logger.info("Shutting down")
    finally:
        collector.stop()
        queue_writer.stop()
        obd_connection.disconnect()
        conn.close()


if __name__ == "__main__":
    main()
