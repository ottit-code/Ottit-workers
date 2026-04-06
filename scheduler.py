"""
scheduler.py — main entry point

Runs all pollers on their schedules:
- stats_poller: every 6 hours
- delivery_poller: every 2 hours
- notifier: every 15 minutes
"""

import logging
import time
from apscheduler.schedulers.blocking import BlockingScheduler
from workers import stats_poller, delivery_poller, notifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

scheduler = BlockingScheduler()


def main():
    # Run immediately on startup
    logger.info("Running initial poll on startup")
    stats_poller.run()
    delivery_poller.run()
    notifier.run()

    # Schedule recurring jobs
    scheduler.add_job(stats_poller.run, "interval", hours=6, id="stats_poller")
    scheduler.add_job(delivery_poller.run, "interval", hours=2, id="delivery_poller")
    scheduler.add_job(notifier.run, "interval", minutes=15, id="notifier")

    logger.info("Scheduler started")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")


if __name__ == "__main__":
    main()
