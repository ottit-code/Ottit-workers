"""
notifier.py — alert rule engine, runs every 15 minutes

Checks alert conditions and writes to the notifications table.
Supabase Realtime then pushes changes to the dashboard.
"""

import logging
import os
import smtplib
from datetime import date
from email.mime.text import MIMEText
from lib.supabase_client import get_supabase
from lib.notifications import create_notification

logger = logging.getLogger(__name__)


def _already_notified_today(type_: str, entity_id: str = None) -> bool:
    """Check if we already sent this type of notification today to avoid spam."""
    supabase = get_supabase()
    today = date.today().isoformat()
    query = supabase.table("notifications").select("id").eq("type", type_).gte("created_at", today)
    if entity_id:
        query = query.eq("entity_id", entity_id)
    result = query.limit(1).execute()
    return len(result.data) > 0


def check_bounce_rate_spike() -> None:
    """Alert if any sender has bounce rate > 5% today."""
    supabase = get_supabase()
    today = date.today().isoformat()
    try:
        result = supabase.table("sender_daily_stats").select("*").eq("stat_date", today).execute()
        for row in result.data:
            sent = row.get("emails_sent", 0) or 0
            bounced = row.get("emails_bounced", 0) or 0
            if sent > 0 and bounced / sent > 0.05:
                # sender_email_id is integer in schema; convert to str for entity_id
                entity_id = str(row["sender_email_id"])
                if not _already_notified_today("bounce_spike", entity_id):
                    pct = f"{(bounced/sent*100):.1f}%"
                    create_notification(
                        severity="warning",
                        type_="bounce_spike",
                        title=f"High bounce rate: {row.get('sender_email', entity_id)}",
                        body=f"Bounce rate is {pct} today (threshold: 5%).",
                        entity_type="sender",
                        entity_id=entity_id,
                    )
    except Exception as e:
        logger.error(f"check_bounce_rate_spike failed: {e}")


def check_daily_limit_approaching() -> None:
    """Alert if sender is at 90%+ of daily limit."""
    supabase = get_supabase()
    today = date.today().isoformat()
    try:
        result = supabase.table("sender_daily_stats").select("*").eq("stat_date", today).execute()
        for row in result.data:
            limit = row.get("daily_limit", 0) or 0
            sent = row.get("emails_sent", 0) or 0
            if limit > 0 and sent / limit >= 0.9:
                # sender_email_id is integer in schema; convert to str for entity_id
                entity_id = str(row["sender_email_id"])
                if not _already_notified_today("daily_limit_approaching", entity_id):
                    pct = f"{(sent/limit*100):.0f}%"
                    create_notification(
                        severity="info",
                        type_="daily_limit_approaching",
                        title=f"Sender near daily limit: {row.get('sender_email', entity_id)}",
                        body=f"Used {sent}/{limit} sends today ({pct}).",
                        entity_type="sender",
                        entity_id=entity_id,
                    )
    except Exception as e:
        logger.error(f"check_daily_limit_approaching failed: {e}")


def check_spam_score() -> None:
    """Alert if latest spam filter test score > 5.0."""
    supabase = get_supabase()
    try:
        result = supabase.table("spam_filter_tests").select("*").order(
            "created_at", desc=True
        ).limit(20).execute()
        for row in result.data:
            score = row.get("score") or 0
            if score > 5.0:
                entity_id = row.get("eg_test_uuid", "")
                if not _already_notified_today("spam_score_high", entity_id):
                    create_notification(
                        severity="warning",
                        type_="spam_score_high",
                        title=f"High spam score: {row.get('domain', 'unknown')}",
                        body=f"Spam filter score is {score} (threshold: 5.0).",
                        entity_type="domain",
                        entity_id=entity_id,
                    )
    except Exception as e:
        logger.error(f"check_spam_score failed: {e}")


def run() -> None:
    """Main entry point called by the scheduler."""
    logger.info("Running notifier checks")
    for fn in [check_bounce_rate_spike, check_daily_limit_approaching, check_spam_score]:
        try:
            fn()
        except Exception as e:
            logger.error(f"notifier.{fn.__name__} failed: {e}")
    logger.info("Notifier complete")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
