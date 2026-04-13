"""
notifier.py — alert rule engine, runs every 15 minutes

Checks alert conditions and writes to the notifications table.
Supabase Realtime then pushes changes to the dashboard.
"""

import logging
from datetime import datetime, timezone
from lib.supabase_client import get_supabase
from lib.notifications import create_notification

logger = logging.getLogger(__name__)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _load_todays_notifications() -> set:
    """
    Return a set of (type, entity_id) pairs already notified today.
    Used for in-memory dedup instead of N per-row DB queries.
    """
    supabase = get_supabase()
    try:
        result = (
            supabase.table("notifications")
            .select("type,entity_id")
            .gte("created_at", _today())
            .execute()
        )
        return {(r["type"], r.get("entity_id")) for r in result.data}
    except Exception as e:
        logger.error(f"_load_todays_notifications failed: {e}")
        return set()


def check_bounce_rate_spike(sender_rows: list, notified: set) -> None:
    """Alert if any sender has bounce rate > 5% today."""
    for row in sender_rows:
        sent = row.get("emails_sent", 0) or 0
        bounced = row.get("emails_bounced", 0) or 0
        if sent > 0 and bounced / sent > 0.05:
            entity_id = str(row["sender_email_id"])
            if ("bounce_spike", entity_id) not in notified:
                pct = f"{(bounced / sent * 100):.1f}%"
                create_notification(
                    severity="warning",
                    type_="bounce_spike",
                    title=f"High bounce rate: {row.get('sender_email', entity_id)}",
                    body=f"Bounce rate is {pct} today (threshold: 5%).",
                    entity_type="sender",
                    entity_id=entity_id,
                )
                notified.add(("bounce_spike", entity_id))


def check_daily_limit_approaching(sender_rows: list, notified: set) -> None:
    """Alert if sender is at 90%+ of daily limit."""
    for row in sender_rows:
        limit = row.get("daily_limit", 0) or 0
        sent = row.get("emails_sent", 0) or 0
        if limit > 0 and sent / limit >= 0.9:
            entity_id = str(row["sender_email_id"])
            if ("daily_limit_approaching", entity_id) not in notified:
                pct = f"{(sent / limit * 100):.0f}%"
                create_notification(
                    severity="info",
                    type_="daily_limit_approaching",
                    title=f"Sender near daily limit: {row.get('sender_email', entity_id)}",
                    body=f"Used {sent}/{limit} sends today ({pct}).",
                    entity_type="sender",
                    entity_id=entity_id,
                )
                notified.add(("daily_limit_approaching", entity_id))


def check_spam_score(notified: set) -> None:
    """Alert if any spam filter test completed today has score > 5.0."""
    supabase = get_supabase()
    try:
        result = (
            supabase.table("spam_filter_tests")
            .select("eg_test_uuid,domain,score")
            .gte("created_at", _today())
            .order("created_at", desc=True)
            .limit(20)
            .execute()
        )
        for row in result.data:
            score = row.get("score") or 0
            if score > 5.0:
                entity_id = row.get("eg_test_uuid", "")
                if ("spam_score_high", entity_id) not in notified:
                    create_notification(
                        severity="warning",
                        type_="spam_score_high",
                        title=f"High spam score: {row.get('domain', 'unknown')}",
                        body=f"Spam filter score is {score} (threshold: 5.0).",
                        entity_type="domain",
                        entity_id=entity_id,
                    )
                    notified.add(("spam_score_high", entity_id))
    except Exception as e:
        logger.error(f"check_spam_score failed: {e}")


def run() -> None:
    """Main entry point called by the scheduler."""
    logger.info("Running notifier checks")

    # Load sender stats once — shared across both sender checks
    supabase = get_supabase()
    try:
        sender_rows = (
            supabase.table("sender_daily_stats")
            .select("sender_email_id,sender_email,emails_sent,emails_bounced,daily_limit")
            .eq("stat_date", _today())
            .execute()
            .data
        )
    except Exception as e:
        logger.error(f"notifier: failed to load sender_daily_stats: {e}")
        sender_rows = []

    # Load today's notifications once for in-memory dedup
    notified = _load_todays_notifications()

    for fn, args in [
        (check_bounce_rate_spike, (sender_rows, notified)),
        (check_daily_limit_approaching, (sender_rows, notified)),
        (check_spam_score, (notified,)),
    ]:
        try:
            fn(*args)
        except Exception as e:
            logger.error(f"notifier.{fn.__name__} failed: {e}")

    logger.info("Notifier complete")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
