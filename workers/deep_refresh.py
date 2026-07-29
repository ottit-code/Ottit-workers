"""
deep_refresh.py — scheduled full data refresh

Runs the same read-only sources as the manual POST /actions/refresh, but
sequentially (one poller at a time) so EmailBison/EmailGuard aren't hammered
by six parallel pollers — parallel refreshes were starving live endpoints
like /schedule/today.

Scheduled every 3 hours at 00/03/06/09/12/15/18/21 UTC. The dashboard's
refresh button is disabled and points users at this cadence.
"""

import logging
import time

logger = logging.getLogger(__name__)


def _sources() -> list[tuple]:
    # Imported lazily to keep module import cheap.
    from workers import (
        stats_poller,
        campaign_daily_stats_poller,
        sender_performance_poller,
        reply_events_poller,
        delivery_poller,
        inboxassure_poller,
        send_plan_snapshotter,
    )
    from lib import inboxassure

    sources: list[tuple] = [
        # First: pre-capture tomorrow's send plan so the Daily Review's
        # Tomorrow card serves from Supabase instead of paging Bison live.
        ("tomorrow_send_plan", send_plan_snapshotter.run_tomorrow),
        ("sender_and_workspace_stats", stats_poller.run),
        ("campaign_daily_stats", campaign_daily_stats_poller.run),
        ("sender_performance", sender_performance_poller.run),
        ("reply_events", reply_events_poller.run),
        ("deliverability_results", delivery_poller.run),
    ]
    if inboxassure.is_configured():
        sources.append(("inboxassure_placement", inboxassure_poller.run))
    return sources


def run() -> None:
    """Main entry point called by the scheduler."""
    logger.info("Deep refresh starting (sequential)")
    for name, fn in _sources():
        started = time.time()
        try:
            fn()
            logger.info(f"deep_refresh: {name} done in {time.time() - started:.0f}s")
        except Exception as e:
            logger.error(f"deep_refresh: {name} failed after {time.time() - started:.0f}s: {e}")
    logger.info("Deep refresh complete")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
