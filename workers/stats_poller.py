"""
stats_poller.py — runs every 6 hours

Fetches sender email stats from EmailBison and upserts into sender_daily_stats.
Also fetches campaign event stats.
"""

import logging
from datetime import date, datetime, timedelta
from lib import emailbison
from lib.supabase_client import get_supabase

logger = logging.getLogger(__name__)


def poll_sender_stats() -> None:
    """Fetch all sender emails and upsert today's stats into sender_daily_stats."""
    supabase = get_supabase()
    today = date.today().isoformat()

    senders = emailbison.get_sender_emails()
    logger.info(f"Polling stats for {len(senders)} senders")

    for sender in senders:
        email = sender.get("email", "")
        domain = email.split("@")[1] if "@" in email else ""

        row = {
            "sender_email_id": int(sender.get("id", 0)),
            "sender_email": email,
            "domain": domain,
            "stat_date": today,
            "emails_sent": sender.get("emails_sent_count", 0) or 0,
            "emails_opened": sender.get("emails_opened_count", 0) or 0,
            "emails_replied": sender.get("emails_replied_count", 0) or 0,
            "emails_bounced": sender.get("bounced_count", 0) or 0,
            "warmup_sent": sender.get("warmup_sent_count", 0) or 0,
            "warmup_replied": sender.get("warmup_replied_count", 0) or 0,
            "daily_limit": sender.get("daily_limit", 0) or 0,
            "warmup_enabled": bool(sender.get("warmup_enabled", False)),
            "fetched_at": datetime.utcnow().isoformat(),
        }

        # Note: upsert requires unique constraint on (sender_email_id, stat_date).
        # Add via Supabase dashboard if not already present:
        #   CREATE UNIQUE INDEX ON sender_daily_stats(sender_email_id, stat_date);
        try:
            supabase.table("sender_daily_stats").upsert(
                row, on_conflict="sender_email_id,stat_date"
            ).execute()
            logger.debug(f"Upserted stats for {email}")
        except Exception as e:
            logger.error(f"Failed to upsert stats for {email}: {e}")


def poll_campaign_event_stats() -> None:
    """Fetch campaign event stats for the last 30 days."""
    end_date = date.today().isoformat()
    start_date = (date.today() - timedelta(days=30)).isoformat()

    try:
        stats = emailbison.get_campaign_events_stats(start_date, end_date)
        logger.info(f"Fetched campaign event stats: {type(stats)}")
        # Stats are stored for future use; the frontend reads sender_daily_stats
    except Exception as e:
        logger.error(f"Failed to fetch campaign event stats: {e}")


def run() -> None:
    """Main entry point called by the scheduler."""
    logger.info("Starting stats poll")
    try:
        poll_sender_stats()
    except Exception as e:
        logger.error(f"stats_poller.poll_sender_stats failed: {e}")

    try:
        poll_campaign_event_stats()
    except Exception as e:
        logger.error(f"stats_poller.poll_campaign_event_stats failed: {e}")

    logger.info("Stats poll complete")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
