"""
scheduler.py — main entry point

Runs all pollers on their schedules:
- deep_refresh:                  every 3 hours at 00/03/06/09/12/15/18/21 UTC
                                 (stats, campaign daily stats, sender
                                 performance, reply events, deliverability,
                                 InboxAssure — run sequentially)
- notifier:                      every 15 minutes
- ab_test_snapshots_poller:      every 6 hours
- lead_engagement_poller:        daily at 2 AM
- domain_blacklist_poller:       every 12 hours
- dns_check_poller:              every 12 hours
- placement_schedule_runner:     every 15 minutes
- send_plan_snapshotter:         daily at 00:05 UTC (captures the day's plan)

NOTE: The action API server must be run separately:
  uvicorn api.main:app --host 0.0.0.0 --port 8000
"""

import logging
from apscheduler.schedulers.blocking import BlockingScheduler
from workers import (
    deep_refresh,
    notifier,
    ab_test_snapshots_poller,
    lead_engagement_poller,
    domain_blacklist_poller,
    dns_check_poller,
    placement_schedule_runner,
    send_plan_snapshotter,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

scheduler = BlockingScheduler()

# Fixed refresh slots (UTC). The dashboard's refresh button shows this cadence.
REFRESH_HOURS = "0,3,6,9,12,15,18,21"


def main():
    # Run immediately on startup
    logger.info("Running initial poll on startup")
    deep_refresh.run()
    notifier.run()
    ab_test_snapshots_poller.run()
    domain_blacklist_poller.run()
    dns_check_poller.run()
    # lead_engagement_poller is intentionally skipped on startup — 48 k leads
    # is expensive; let the scheduled 2 AM run handle it.

    # Schedule recurring jobs — max_instances=1 + coalesce prevent stacked runs
    # if a job takes longer than its interval.
    scheduler.add_job(deep_refresh.run, "cron", hour=REFRESH_HOURS, minute=0,
                      id="deep_refresh", max_instances=1, coalesce=True)
    scheduler.add_job(notifier.run, "interval", minutes=15,
                      id="notifier", max_instances=1, coalesce=True)
    scheduler.add_job(ab_test_snapshots_poller.run, "interval", hours=6,
                      id="ab_test_snapshots_poller", max_instances=1, coalesce=True)
    scheduler.add_job(lead_engagement_poller.run, "cron", hour=2, minute=0,
                      id="lead_engagement_poller", max_instances=1, coalesce=True)
    scheduler.add_job(domain_blacklist_poller.run, "interval", hours=12,
                      id="domain_blacklist_poller", max_instances=1, coalesce=True)
    scheduler.add_job(dns_check_poller.run, "interval", hours=12,
                      id="dns_check_poller", max_instances=1, coalesce=True)
    scheduler.add_job(placement_schedule_runner.run, "interval", minutes=15,
                      id="placement_schedule_runner", max_instances=1, coalesce=True)
    # Right after UTC midnight, before sending drains the queue. Not run on
    # startup: a mid-day capture would record an already-drained plan.
    scheduler.add_job(send_plan_snapshotter.run, "cron", hour=0, minute=5,
                      id="send_plan_snapshotter", max_instances=1, coalesce=True)

    logger.info("Scheduler started")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")


if __name__ == "__main__":
    main()
