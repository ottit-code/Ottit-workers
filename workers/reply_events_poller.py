"""
reply_events_poller.py — runs every 4 hours

Fetches classified reply events from EmailBison for all active campaigns
and upserts into Supabase:
- reply_events: per-reply record with classification, timing, and metadata

Uses ON CONFLICT (reply_id) so already-ingested replies are idempotent.
"""

import logging
from datetime import datetime, timezone

from lib import emailbison
from lib.supabase_client import get_supabase
from lib.utils import get_active_campaign_ids

logger = logging.getLogger(__name__)

_CLASSIFICATIONS = ["interested", "not_automated_reply", "automated_reply"]


def _compute_response_time_hours(
    replied_at: str | None, sent_at: str | None
) -> float | None:
    """Return hours between sent and replied timestamps, or None if either is missing."""
    if not replied_at or not sent_at:
        return None
    try:
        def _parse(s: str) -> datetime:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))

        delta = (_parse(replied_at) - _parse(sent_at)).total_seconds()
        return round(delta / 3600, 4) if delta >= 0 else None
    except Exception:
        return None


def poll_reply_events() -> None:
    """Fetch classified replies for all active campaigns and upsert into reply_events."""
    supabase = get_supabase()
    campaign_ids = get_active_campaign_ids(supabase)
    logger.info(f"Polling reply events for {len(campaign_ids)} campaigns")

    all_rows: list[dict] = []
    seen_ids: set[str] = set()  # Deduplicate across classification buckets

    for campaign_id in campaign_ids:
        for classification in _CLASSIFICATIONS:
            try:
                replies = emailbison.get_campaign_replies(campaign_id, classification)
                for reply in replies:
                    reply_id = str(reply.get("id") or "")
                    if not reply_id or reply_id in seen_ids:
                        continue
                    seen_ids.add(reply_id)

                    lead = reply.get("lead") or {}
                    sender = reply.get("sender_email") or {}
                    campaign = reply.get("campaign") or {}
                    scheduled = reply.get("scheduled_email") or {}
                    sent_at = scheduled.get("sent_at")
                    replied_at = reply.get("replied_at")

                    # Integer fields: fall back to top-level reply fields, never use ""
                    lead_id = lead.get("id") or reply.get("lead_id") or None
                    sender_email_id = sender.get("id") or reply.get("sender_email_id") or None
                    seq_step_id = scheduled.get("sequence_step_id") or None

                    all_rows.append({
                        "reply_id": reply_id,
                        "campaign_id": str(campaign.get("id") or reply.get("campaign_id") or campaign_id),
                        "campaign_name": campaign.get("name"),
                        "lead_id": lead_id,
                        "lead_email": lead.get("email") or reply.get("from_email_address"),
                        "sender_email_id": sender_email_id,
                        "sender_email": sender.get("email"),
                        "sequence_step_id": seq_step_id,
                        "classification": classification,
                        "folder": reply.get("folder"),
                        "replied_at": replied_at,
                        "original_sent_at": sent_at,
                        "response_time_hours": _compute_response_time_hours(
                            replied_at, sent_at
                        ),
                        "subject": reply.get("subject"),
                        "has_attachment": bool(reply.get("has_attachments", False)),
                        "is_thread_reply": bool(reply.get("thread_reply", False)),
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                    })
            except Exception as e:
                logger.error(
                    f"Failed to fetch {classification} replies "
                    f"for campaign {campaign_id}: {e}"
                )

    if all_rows:
        try:
            supabase.table("reply_events").upsert(
                all_rows, on_conflict="reply_id"
            ).execute()
            logger.info(f"Batch-upserted {len(all_rows)} reply events")
        except Exception as e:
            logger.error(f"Failed to batch-upsert reply events: {e}")


def run() -> None:
    """Main entry point called by the scheduler."""
    logger.info("Starting reply events poll")
    try:
        poll_reply_events()
    except Exception as e:
        logger.error(f"reply_events_poller.poll_reply_events failed: {e}")
    logger.info("Reply events poll complete")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
