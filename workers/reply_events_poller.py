"""
reply_events_poller.py — runs every 4 hours

Fetches classified reply events from EmailBison for all active campaigns
and upserts into Supabase:
- reply_events: per-reply record with classification, timing, and metadata

Uses ON CONFLICT (reply_id) so already-ingested replies are idempotent.

original_sent_at (cohort reply attribution): the replies payload only
carries scheduled_email_id — never the sent timestamp — so we resolve it
via GET /api/scheduled-emails/{id}. Resolved values are preserved across
runs and lookups are capped per run, so the historical backlog backfills
over successive runs without hammering the Bison API.
"""

import logging
from datetime import datetime, timezone

import httpx

from lib import emailbison
from lib.config import DEFAULT_WORKSPACE_ID, pollable_workspaces
from lib.supabase_client import get_supabase
from lib.utils import get_active_campaign_ids_from_bison

logger = logging.getLogger(__name__)

_CLASSIFICATIONS = ["interested", "not_automated_reply", "automated_reply"]

# Max scheduled-email detail fetches per run (per workspace). Each resolves
# original_sent_at for one reply; the rest are picked up on later runs.
_MAX_SENT_LOOKUPS_PER_RUN = 1500


def _existing_sent_map(supabase, workspace_id: str) -> dict[str, str]:
    """reply_id → original_sent_at for rows already resolved (paginated)."""
    resolved: dict[str, str] = {}
    offset, page = 0, 1000
    while True:
        rows = (
            supabase.table("reply_events")
            .select("reply_id,original_sent_at")
            .eq("workspace_id", workspace_id)
            .not_.is_("original_sent_at", "null")
            .range(offset, offset + page - 1)
            .execute()
            .data or []
        )
        for r in rows:
            resolved[str(r["reply_id"])] = r["original_sent_at"]
        if len(rows) < page:
            return resolved
        offset += page


def _campaigns_with_unresolved_sent(supabase, workspace_id: str) -> list[str]:
    """Distinct campaign_ids with reply rows still missing original_sent_at."""
    ids: dict[str, None] = {}
    offset, page = 0, 1000
    while True:
        rows = (
            supabase.table("reply_events")
            .select("campaign_id")
            .eq("workspace_id", workspace_id)
            .is_("original_sent_at", "null")
            .range(offset, offset + page - 1)
            .execute()
            .data or []
        )
        for r in rows:
            if r.get("campaign_id") is not None:
                ids[str(r["campaign_id"])] = None
        if len(rows) < page:
            return list(ids)
        offset += page


def _fetch_scheduled_email(bison, scheduled_email_id, cache: dict) -> dict:
    """Scheduled-email detail (has sent_at / sequence_step_id), cached per run."""
    if scheduled_email_id in cache:
        return cache[scheduled_email_id]
    try:
        data = bison.get(f"/api/scheduled-emails/{scheduled_email_id}")
        if isinstance(data, dict):
            data = data.get("data", data)
        cache[scheduled_email_id] = data if isinstance(data, dict) else {}
    except Exception as e:
        logger.debug(f"scheduled-email {scheduled_email_id} lookup failed: {e}")
        cache[scheduled_email_id] = {}
    return cache[scheduled_email_id]


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


def poll_reply_events(workspace_id: str = DEFAULT_WORKSPACE_ID, bison=None) -> None:
    """Fetch classified replies for all active campaigns and upsert into reply_events."""
    supabase = get_supabase()
    bison = bison or emailbison
    # Live Bison is the source of truth — the documents-table campaign list
    # can be stale (deleted campaigns 404). Also revisit campaigns that still
    # have unresolved original_sent_at rows so history backfills.
    campaign_ids = get_active_campaign_ids_from_bison(bison)
    campaign_ids = list(dict.fromkeys(
        campaign_ids + _campaigns_with_unresolved_sent(supabase, workspace_id)
    ))
    logger.info(f"[{workspace_id}] Polling reply events for {len(campaign_ids)} campaigns")

    # reply_id → already-resolved original_sent_at, so re-upserts never null
    # it out and we don't re-fetch the scheduled email on every run.
    existing_sent = _existing_sent_map(supabase, workspace_id)

    all_rows: list[dict] = []
    seen_ids: set[str] = set()  # Deduplicate across classification buckets
    scheduled_cache: dict = {}
    sent_lookups = 0

    for campaign_id in campaign_ids:
        for classification in _CLASSIFICATIONS:
            try:
                replies = bison.get_campaign_replies(campaign_id, classification)
                for reply in replies:
                    reply_id = str(reply.get("id") or "")
                    if not reply_id or reply_id in seen_ids:
                        continue
                    seen_ids.add(reply_id)

                    lead = reply.get("lead") or {}
                    sender = reply.get("sender_email") or {}
                    campaign = reply.get("campaign") or {}
                    scheduled = reply.get("scheduled_email") or {}

                    # EmailBison returns `date_received` on /api/campaigns/{id}/replies;
                    # `replied_at` is only present on the legacy /api/replies list.
                    replied_at = (
                        scheduled.get("replied_at")
                        or reply.get("replied_at")
                        or reply.get("date_received")
                        or reply.get("created_at")
                    )
                    sent_at = scheduled.get("sent_at") or existing_sent.get(reply_id)

                    lead_id = lead.get("id") or reply.get("lead_id") or None
                    sender_email_id = sender.get("id") or reply.get("sender_email_id") or None
                    seq_step_id = (
                        scheduled.get("sequence_step_id")
                        or reply.get("sequence_step_id")
                        or None
                    )

                    # Resolve the ORIGINAL email's sent timestamp from the
                    # scheduled-email detail — the replies payload never
                    # includes it. Capped per run; the rest backfill later.
                    scheduled_email_id = reply.get("scheduled_email_id")
                    if (
                        not sent_at
                        and scheduled_email_id
                        and sent_lookups < _MAX_SENT_LOOKUPS_PER_RUN
                    ):
                        detail = _fetch_scheduled_email(bison, scheduled_email_id, scheduled_cache)
                        sent_lookups += 1
                        sent_at = detail.get("sent_at")
                        seq_step_id = seq_step_id or detail.get("sequence_step_id")
                        detail_sender = detail.get("sender_email") or {}
                        if isinstance(detail_sender, dict):
                            sender_email_id = sender_email_id or detail_sender.get("id")
                            if not sender.get("email"):
                                sender = {**sender, "email": detail_sender.get("email")}

                    all_rows.append({
                        "workspace_id": workspace_id,
                        "reply_id": reply_id,
                        "campaign_id": str(campaign.get("id") or reply.get("campaign_id") or campaign_id),
                        "campaign_name": campaign.get("name"),
                        "lead_id": lead_id,
                        "lead_email": lead.get("email") or reply.get("from_email_address"),
                        "sender_email_id": sender_email_id,
                        "sender_email": sender.get("email") or reply.get("primary_to_email_address"),
                        "sequence_step_id": seq_step_id,
                        "classification": classification,
                        "folder": reply.get("folder"),
                        "replied_at": replied_at,
                        "original_sent_at": sent_at,
                        "response_time_hours": _compute_response_time_hours(
                            replied_at, sent_at
                        ),
                        "subject": reply.get("subject"),
                        "has_attachment": bool(reply.get("attachments") or reply.get("has_attachments")),
                        "is_thread_reply": bool(reply.get("parent_id") or reply.get("thread_reply")),
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                    })
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    logger.warning(f"Campaign {campaign_id} not found (404) — may have been deleted from EmailBison")
                    break  # No point trying other classifications for this campaign
                else:
                    logger.error(f"Failed to fetch {classification} replies for campaign {campaign_id}: {e}")
            except Exception as e:
                logger.error(
                    f"Failed to fetch {classification} replies "
                    f"for campaign {campaign_id}: {e}"
                )

    if sent_lookups:
        resolved = sum(1 for r in all_rows if r["original_sent_at"])
        logger.info(
            f"[{workspace_id}] Resolved sent_at via {sent_lookups} scheduled-email lookups "
            f"({resolved}/{len(all_rows)} rows have original_sent_at)"
        )

    if all_rows:
        try:
            supabase.table("reply_events").upsert(
                all_rows, on_conflict="workspace_id,reply_id"
            ).execute()
            logger.info(f"[{workspace_id}] Batch-upserted {len(all_rows)} reply events")
        except Exception as e:
            # Pre-migration-012 databases only have the (reply_id) unique key.
            logger.warning(
                f"[{workspace_id}] workspace-scoped upsert failed ({e}); retrying on reply_id"
            )
            try:
                supabase.table("reply_events").upsert(
                    all_rows, on_conflict="reply_id"
                ).execute()
                logger.info(f"[{workspace_id}] Batch-upserted {len(all_rows)} reply events (legacy key)")
            except Exception as e2:
                logger.error(f"[{workspace_id}] Failed to batch-upsert reply events: {e2}")


def run() -> None:
    """Main entry point called by the scheduler."""
    logger.info("Starting reply events poll")
    for ws in pollable_workspaces():
        try:
            bison = emailbison.for_workspace(ws["id"])
        except Exception as e:
            logger.error(f"Skipping workspace {ws['id']}: {e}")
            continue
        try:
            poll_reply_events(ws["id"], bison)
        except Exception as e:
            logger.error(f"[{ws['id']}] reply_events_poller.poll_reply_events failed: {e}")
    logger.info("Reply events poll complete")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
