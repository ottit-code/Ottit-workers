"""
lead_engagement_poller.py — runs daily at 2 AM

Paginates all leads from EmailBison and upserts into Supabase:
- lead_engagement_snapshots: per-lead engagement stats, funnel stage, JSONB fields

Rate limiting: 300ms delay between pages (~483 calls for 48 k leads).
"""

import logging
import time
from datetime import datetime, timezone

from lib import emailbison
from lib.supabase_client import get_supabase

logger = logging.getLogger(__name__)

_PAGE_DELAY_S = 0.3   # 300ms between pages to respect rate limits
_PER_PAGE = 100


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _compute_engagement_score(replies: int, unique_opens: int, opens: int) -> int:
    """Engagement score = replies×5 + unique_opens×2 + opens×1."""
    return (replies * 5) + (unique_opens * 2) + opens


def _compute_funnel_stage(
    emails_sent: int,
    opens: int,
    replies: int,
    campaign_data: list,
) -> str:
    """Determine highest funnel stage reached by this lead."""
    if any(c.get("interested") for c in campaign_data):
        return "interested"
    if replies > 0:
        return "replied"
    if opens > 0:
        return "opened"
    if emails_sent > 0:
        return "contacted"
    return "uploaded"


def _build_custom_variables(variables: list) -> dict:
    """Convert [{name, value}] array to {name: value} dict for JSONB storage."""
    result: dict = {}
    for var in variables or []:
        name = var.get("name") or var.get("key")
        if name:
            result[name] = var.get("value")
    return result


def poll_lead_engagement() -> None:
    """Paginate through all leads and upsert today's engagement snapshot."""
    supabase = get_supabase()
    today = _today()

    page = 1
    total_processed = 0

    while True:
        try:
            raw = emailbison.get_leads_paginated(page=page, per_page=_PER_PAGE)
        except Exception as e:
            logger.error(f"Failed to fetch leads page {page}: {e}")
            break

        # Normalise response: may be {data: [...], meta: {...}} or plain list
        if isinstance(raw, list):
            leads = raw
            last_page = 1
        elif isinstance(raw, dict):
            leads = raw.get("data", [])
            meta = raw.get("meta") or {}
            last_page = int(meta.get("last_page") or 1)
        else:
            logger.error(f"Unexpected leads response type: {type(raw)}")
            break

        if not leads:
            break

        logger.info(f"Processing leads page {page}/{last_page} ({len(leads)} leads)")

        rows: list[dict] = []
        fetched_at = datetime.now(timezone.utc).isoformat()

        for lead in leads:
            lead_id = lead.get("id")
            if not lead_id:
                continue

            overall = lead.get("overall_stats") or {}
            emails_sent = int(overall.get("emails_sent") or 0)
            opens = int(overall.get("opens") or 0)
            unique_opens = int(overall.get("unique_opens") or 0)
            replies = int(overall.get("replies") or 0)
            unique_replies = int(overall.get("unique_replies") or 0)

            campaign_data = lead.get("lead_campaign_data") or []

            rows.append({
                "lead_id": str(lead_id),
                "snapshot_date": today,
                "first_name": lead.get("first_name"),
                "last_name": lead.get("last_name"),
                "email": lead.get("email"),
                "title": lead.get("title"),
                "company": lead.get("company"),
                "lead_status": lead.get("status"),
                "tags": lead.get("tags"),
                "emails_sent": emails_sent,
                "opens": opens,
                "unique_opens": unique_opens,
                "replies": replies,
                "unique_replies": unique_replies,
                "engagement_score": _compute_engagement_score(replies, unique_opens, opens),
                "funnel_stage": _compute_funnel_stage(emails_sent, opens, replies, campaign_data),
                "campaign_engagements": campaign_data,
                "custom_variables": _build_custom_variables(lead.get("custom_variables")),
                "fetched_at": fetched_at,
            })

        if rows:
            try:
                supabase.table("lead_engagement_snapshots").upsert(
                    rows, on_conflict="lead_id,snapshot_date"
                ).execute()
                logger.info(
                    f"Batch-upserted {len(rows)} lead engagement rows (page {page})"
                )
            except Exception as e:
                logger.error(
                    f"Failed to batch-upsert lead engagement rows (page {page}): {e}"
                )

        total_processed += len(leads)

        if page >= last_page:
            break
        page += 1
        time.sleep(_PAGE_DELAY_S)

    logger.info(f"Lead engagement poll complete — {total_processed} leads processed")


def run() -> None:
    """Main entry point called by the scheduler."""
    logger.info("Starting lead engagement poll")
    try:
        poll_lead_engagement()
    except Exception as e:
        logger.error(f"lead_engagement_poller.poll_lead_engagement failed: {e}")
    logger.info("Lead engagement poll complete")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
