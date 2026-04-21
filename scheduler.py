"""
scheduler.py — main entry point

Runs all pollers on their schedules:
- stats_poller:                  every 6 hours
- delivery_poller:               every 2 hours
- notifier:                      every 15 minutes
- ab_test_snapshots_poller:      every 6 hours
- reply_events_poller:           every 4 hours
- campaign_daily_stats_poller:   daily at midnight
- sender_performance_poller:     daily at 1 AM
- lead_engagement_poller:        daily at 2 AM
- domain_blacklist_poller:       every 12 hours

NOTE: The action API server must be run separately:
  uvicorn api.main:app --host 0.0.0.0 --port 8000
"""

import logging
import time
from apscheduler.schedulers.blocking import BlockingScheduler
from workers import (
    stats_poller,
    delivery_poller,
    notifier,
    ab_test_snapshots_poller,
    campaign_daily_stats_poller,
    lead_engagement_poller,
    reply_events_poller,
    sender_performance_poller,
    domain_blacklist_poller,
)

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
    ab_test_snapshots_poller.run()
    campaign_daily_stats_poller.run()
    reply_events_poller.run()
    sender_performance_poller.run()
    domain_blacklist_poller.run()
    # lead_engagement_poller is intentionally skipped on startup — 48 k leads
    # is expensive; let the scheduled 2 AM run handle it.

    # Schedule recurring jobs — max_instances=1 + coalesce prevent stacked runs
    # if a job takes longer than its interval.
    scheduler.add_job(stats_poller.run, "interval", hours=6,
                      id="stats_poller", max_instances=1, coalesce=True)
    scheduler.add_job(delivery_poller.run, "interval", hours=2,
                      id="delivery_poller", max_instances=1, coalesce=True)
    scheduler.add_job(notifier.run, "interval", minutes=15,
                      id="notifier", max_instances=1, coalesce=True)

    # New pollers
    scheduler.add_job(ab_test_snapshots_poller.run, "interval", hours=6,
                      id="ab_test_snapshots_poller", max_instances=1, coalesce=True)
    scheduler.add_job(reply_events_poller.run, "interval", hours=4,
                      id="reply_events_poller", max_instances=1, coalesce=True)
    scheduler.add_job(campaign_daily_stats_poller.run, "cron", hour=0, minute=0,
                      id="campaign_daily_stats_poller", max_instances=1, coalesce=True)
    scheduler.add_job(sender_performance_poller.run, "cron", hour=1, minute=0,
                      id="sender_performance_poller", max_instances=1, coalesce=True)
    scheduler.add_job(lead_engagement_poller.run, "cron", hour=2, minute=0,
                      id="lead_engagement_poller", max_instances=1, coalesce=True)
    scheduler.add_job(domain_blacklist_poller.run, "interval", hours=12,
                      id="domain_blacklist_poller", max_instances=1, coalesce=True)

    logger.info("Scheduler started")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")


if __name__ == "__main__":
    main()
