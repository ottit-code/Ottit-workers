"""
stats_poller.py — runs every 6 hours

Fetches data from EmailBison and upserts into Supabase:
- sender_daily_stats: per-sender email stats
- workspace_daily_stats: workspace-wide chart data

Loops over every configured workspace (lib.config.WORKSPACES), using that
workspace's Bison token and stamping workspace_id on every row.
"""

import logging
from datetime import datetime, timedelta, timezone
from lib import emailbison
from lib.config import pollable_workspaces
from lib.supabase_client import get_supabase

logger = logging.getLogger(__name__)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def poll_sender_stats(workspace_id: str, bison: emailbison.BisonClient) -> None:
    """Fetch all sender emails and batch-upsert today's stats into sender_daily_stats.

    Bison's /sender-emails payload names its counters total_replied_count /
    total_opened_count (NOT emails_replied_count / emails_opened_count — reading
    those wrote 0 replies for every sender until 2026-07-29). Warmup counters
    live on the separate /warmup/sender-emails payload, merged in by sender id.
    """
    supabase = get_supabase()
    today = _today()

    senders = bison.get_sender_emails()
    logger.info(f"[{workspace_id}] Polling stats for {len(senders)} senders")

    warmup_by_id: dict = {}
    try:
        for w in bison.get_warmup_sender_emails():
            if w.get("id") is not None:
                warmup_by_id[int(w["id"])] = w
    except Exception as e:
        logger.warning(f"[{workspace_id}] warmup sender fetch failed, warmup counters skipped: {e}")

    rows = []
    for sender in senders:
        email = sender.get("email", "")
        domain = email.split("@")[1] if "@" in email else ""
        sender_id = int(sender.get("id") or 0)
        warmup = warmup_by_id.get(sender_id, {})
        rows.append({
            "workspace_id": workspace_id,
            "sender_email_id": sender_id,
            "sender_email": email,
            "domain": domain,
            "stat_date": today,
            "emails_sent": sender.get("emails_sent_count", 0) or 0,
            "emails_opened": sender.get("total_opened_count", 0) or 0,
            "emails_replied": sender.get("total_replied_count", 0) or 0,
            "emails_bounced": sender.get("bounced_count", 0) or 0,
            "warmup_sent": warmup.get("warmup_emails_sent", 0) or 0,
            "warmup_replied": warmup.get("warmup_replies_received", 0) or 0,
            "daily_limit": sender.get("daily_limit", 0) or 0,
            "warmup_enabled": bool(sender.get("warmup_enabled", False)),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        })

    if rows:
        try:
            supabase.table("sender_daily_stats").upsert(
                rows, on_conflict="workspace_id,sender_email_id,stat_date"
            ).execute()
            logger.info(f"[{workspace_id}] Batch-upserted stats for {len(rows)} senders")
        except Exception as e:
            logger.error(f"[{workspace_id}] Failed to batch-upsert sender stats: {e}")


def poll_workspace_stats(workspace_id: str, bison: emailbison.BisonClient) -> None:
    """
    Fetch workspace chart stats from EmailBison and batch-upsert into workspace_daily_stats.
    One row per (workspace, date) with individual metric columns. Backfills the
    last 30 days on each run.
    """
    supabase = get_supabase()
    today = datetime.now(timezone.utc).date()
    end_date = today.isoformat()
    start_date = (today - timedelta(days=30)).isoformat()

    try:
        stats = bison.get_workspace_chart_stats(start_date, end_date)
        series = stats.get("data", [])

        label_to_col = {
            "Sent": "emails_sent",
            "Total Opens": "emails_opened",
            "Replied": "emails_replied",
            "Bounced": "emails_bounced",
            "Unsubscribed": "unsubscribed",
            "Interested": "interested",
        }

        by_date: dict = {}
        for item in series:
            col = label_to_col.get(item.get("label", ""))
            if not col:
                continue
            for date_str, count in item.get("dates", []):
                if date_str not in by_date:
                    by_date[date_str] = {
                        "emails_sent": 0, "emails_opened": 0, "emails_replied": 0,
                        "emails_bounced": 0, "unsubscribed": 0, "interested": 0,
                    }
                by_date[date_str][col] = count or 0

        if by_date:
            fetched_at = datetime.now(timezone.utc).isoformat()
            rows = [
                {
                    "workspace_id": workspace_id,
                    "stat_date": date_str,
                    "fetched_at": fetched_at,
                    **metrics,
                }
                for date_str, metrics in by_date.items()
            ]
            supabase.table("workspace_daily_stats").upsert(
                rows, on_conflict="workspace_id,stat_date"
            ).execute()
            logger.info(f"[{workspace_id}] Batch-upserted workspace stats for {len(rows)} dates")
    except Exception as e:
        logger.error(f"[{workspace_id}] Failed to fetch/store workspace chart stats: {e}")


def run() -> None:
    """Main entry point called by the scheduler."""
    logger.info("Starting stats poll")
    for ws in pollable_workspaces():
        bison = emailbison.for_workspace(ws["id"])
        for fn in [poll_sender_stats, poll_workspace_stats]:
            try:
                fn(ws["id"], bison)
            except Exception as e:
                logger.error(f"stats_poller.{fn.__name__} failed for {ws['id']}: {e}")
    logger.info("Stats poll complete")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
