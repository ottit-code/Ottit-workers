"""
sender_performance_poller.py — runs daily at 1 AM

Fetches sender performance data from EmailBison campaigns, cross-references
deliverability and recovery data from Supabase, and upserts into:
- sender_email_performance: per-sender daily snapshot with health score
"""

import logging
from datetime import datetime, timezone

import httpx

from lib import emailbison
from lib.supabase_client import get_supabase
from lib.utils import get_active_campaign_ids

logger = logging.getLogger(__name__)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _safe_rate(numerator: int | float, denominator: int | float) -> float:
    if not denominator:
        return 0.0
    return round((numerator / denominator) * 100, 4)


def _fetch_sender_lookup_data(supabase, sender_ids: list[int]) -> dict[int, dict]:
    """Batch-fetch warmup scores, recovery status, and deliverability for all senders."""
    if not sender_ids:
        return {}

    lookup: dict[int, dict] = {
        sid: {
            "warmup_score": None,
            "policy_key": None,
            "strike_count": None,
            "next_action_at": None,
            "in_recovery": False,
            "placement_score": None,
            "spam_score": None,
        }
        for sid in sender_ids
    }

    # Latest warmup score per sender (order DESC, take first seen per sid)
    try:
        rows = (
            supabase.table("sender_warmup_history")
            .select("sender_email_id,warmup_score")
            .in_("sender_email_id", sender_ids)
            .order("recorded_at", desc=True)
            .execute()
            .data or []
        )
        for row in rows:
            sid = row.get("sender_email_id")
            if sid in lookup and lookup[sid]["warmup_score"] is None:
                lookup[sid]["warmup_score"] = row.get("warmup_score")
    except Exception as e:
        logger.warning(f"Failed to fetch warmup scores: {e}")

    # Active (incomplete) recovery policies
    try:
        rows = (
            supabase.table("sender_recovery")
            .select("sender_email_id,policy_key,strike_count,next_action_at")
            .in_("sender_email_id", sender_ids)
            .is_("completed_at", "null")
            .execute()
            .data or []
        )
        seen: set[int] = set()
        for row in rows:
            sid = row.get("sender_email_id")
            if sid in lookup and sid not in seen:
                seen.add(sid)
                lookup[sid].update({
                    "in_recovery": True,
                    "policy_key": row.get("policy_key"),
                    "strike_count": row.get("strike_count"),
                    "next_action_at": row.get("next_action_at"),
                })
    except Exception as e:
        logger.warning(f"Failed to fetch recovery data: {e}")

    # Latest inbox placement score per sender
    try:
        rows = (
            supabase.table("domain_placement_tests")
            .select("sender_email_id,overall_score")
            .in_("sender_email_id", sender_ids)
            .order("created_at", desc=True)
            .execute()
            .data or []
        )
        for row in rows:
            sid = row.get("sender_email_id")
            if sid in lookup and lookup[sid]["placement_score"] is None:
                lookup[sid]["placement_score"] = row.get("overall_score")
    except Exception as e:
        logger.warning(f"Failed to fetch placement scores: {e}")

    # Latest spam filter score per sender
    try:
        rows = (
            supabase.table("spam_filter_tests")
            .select("sender_email_id,score")
            .in_("sender_email_id", sender_ids)
            .order("created_at", desc=True)
            .execute()
            .data or []
        )
        for row in rows:
            sid = row.get("sender_email_id")
            if sid in lookup and lookup[sid]["spam_score"] is None:
                lookup[sid]["spam_score"] = row.get("score")
    except Exception as e:
        logger.warning(f"Failed to fetch spam scores: {e}")

    return lookup


def _compute_health_score(supabase, reply_rate: float, bounce_rate: float, db: dict) -> int | None:
    """Call compute_sender_health_score RPC. Returns None if RPC is not available."""
    try:
        result = supabase.rpc("compute_sender_health_score", {
            "warmup_score": db.get("warmup_score"),
            "reply_rate": reply_rate,
            "bounce_rate": bounce_rate,
            "in_recovery": db.get("in_recovery", False),
            "strike_count": db.get("strike_count"),
            "placement_score": db.get("placement_score"),
            "spam_score": db.get("spam_score"),
        }).execute()
        if result.data is not None:
            return result.data
    except Exception as e:
        logger.debug(f"compute_sender_health_score RPC not available: {e}")
    return None


def poll_sender_email_performance() -> None:
    """Fetch email accounts for all active campaigns, deduplicate, and upsert performance rows."""
    supabase = get_supabase()
    today = _today()
    campaign_ids = get_active_campaign_ids(supabase)
    logger.info(f"Polling sender performance across {len(campaign_ids)} campaigns")

    # Collect unique senders across all campaigns
    senders_by_id: dict[int, dict] = {}
    for campaign_id in campaign_ids:
        try:
            accounts = emailbison.get_campaign_email_accounts(campaign_id)
            for account in accounts:
                sid = account.get("id")
                if sid is None:
                    continue
                sid = int(sid)
                if sid not in senders_by_id:
                    senders_by_id[sid] = account
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.warning(f"Campaign {campaign_id} not found (404) — may have been deleted from EmailBison")
            else:
                logger.error(f"Failed to fetch email accounts for campaign {campaign_id}: {e}")
        except Exception as e:
            logger.error(f"Failed to fetch email accounts for campaign {campaign_id}: {e}")

    if not senders_by_id:
        logger.info("No senders found across active campaigns")
        return

    sender_ids = list(senders_by_id.keys())
    logger.info(f"Computing performance for {len(sender_ids)} unique senders")

    lookup = _fetch_sender_lookup_data(supabase, sender_ids)

    fetched_at = datetime.now(timezone.utc).isoformat()
    rows: list[dict] = []

    for sid, account in senders_by_id.items():
        db = lookup.get(sid, {})
        email = account.get("email") or ""
        domain = email.split("@")[1] if "@" in email else (account.get("domain") or "")

        contacts = int(account.get("total_leads_contacted_count") or 0)
        emails_sent = int(account.get("emails_sent_count") or 0)
        unique_replied = int(account.get("unique_replied_count") or 0)
        unique_opened = int(account.get("unique_opened_count") or 0)
        bounced = int(account.get("bounced_count") or 0)
        interested = int(account.get("interested_leads_count") or 0)

        reply_rate = _safe_rate(unique_replied, contacts)
        bounce_rate = _safe_rate(bounced, emails_sent)

        rows.append({
            "sender_email_id": sid,
            "snapshot_date": today,
            "sender_email": email,
            "domain": domain,
            "connection_type": account.get("type"),
            "connection_status": account.get("status"),
            "warmup_enabled": bool(account.get("warmup_enabled", False)),
            "emails_sent_count": emails_sent,
            "total_leads_contacted_count": contacts,
            "total_replied_count": int(account.get("total_replied_count") or 0),
            "total_opened_count": int(account.get("total_opened_count") or 0),
            "unique_replied_count": unique_replied,
            "unique_opened_count": unique_opened,
            "unsubscribed_count": int(account.get("unsubscribed_count") or 0),
            "bounced_count": bounced,
            "interested_leads_count": interested,
            "reply_rate": reply_rate,
            "open_rate": _safe_rate(unique_opened, contacts),
            "bounce_rate": bounce_rate,
            "interest_rate": _safe_rate(interested, contacts),
            "warmup_score": db.get("warmup_score"),
            "in_recovery": db.get("in_recovery", False),
            "recovery_policy_key": db.get("policy_key"),
            "recovery_strike_count": db.get("strike_count"),
            "recovery_next_action_at": db.get("next_action_at"),
            "latest_placement_score": db.get("placement_score"),
            "latest_spam_score": db.get("spam_score"),
            "health_score": _compute_health_score(supabase, reply_rate, bounce_rate, db),
            "tags": account.get("tags"),
            "fetched_at": fetched_at,
        })

    if rows:
        try:
            supabase.table("sender_email_performance").upsert(
                rows, on_conflict="sender_email_id,snapshot_date"
            ).execute()
            logger.info(f"Batch-upserted {len(rows)} sender performance rows")
        except Exception as e:
            logger.error(f"Failed to batch-upsert sender performance: {e}")


def run() -> None:
    """Main entry point called by the scheduler."""
    logger.info("Starting sender email performance poll")
    try:
        poll_sender_email_performance()
    except Exception as e:
        logger.error(
            f"sender_performance_poller.poll_sender_email_performance failed: {e}"
        )
    logger.info("Sender email performance poll complete")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
