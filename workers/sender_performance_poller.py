"""
sender_performance_poller.py — runs daily at 1 AM

Fetches sender performance data from EmailBison campaigns, cross-references
deliverability and recovery data from Supabase, and upserts into:
- sender_email_performance: per-sender daily snapshot with health score
"""

import logging
from datetime import datetime, timedelta, timezone

import httpx

from lib import emailbison
from lib.config import DEFAULT_WORKSPACE_ID, pollable_workspaces
from lib.supabase_client import get_supabase
from lib.supabase_paginate import fetch_all
from lib.utils import get_active_campaign_ids, get_active_campaign_ids_from_bison
from lib.warmup_report import persist_warmup_daily_report

logger = logging.getLogger(__name__)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _safe_rate(numerator: int | float, denominator: int | float) -> float:
    if not denominator:
        return 0.0
    return round((numerator / denominator) * 100, 4)


def _int_score(value) -> int | None:
    """Bison reports warmup_score as a float (e.g. 96.9); our columns are int."""
    if value is None:
        return None
    try:
        return round(float(value))
    except (TypeError, ValueError):
        return None


def _int_counter(value) -> int | None:
    """Coerce Bison warmup counters; None stays None (unknown / missing)."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _warmup_counters(w: dict) -> dict:
    """Map EmailBison /api/warmup/sender-emails fields → DB column names."""
    return {
        "warmup_sent": _int_counter(w.get("warmup_emails_sent")),
        "warmup_replied": _int_counter(w.get("warmup_replies_received")),
        "warmup_saved_from_spam": _int_counter(w.get("warmup_emails_saved_from_spam")),
        "warmup_bounces_received": _int_counter(w.get("warmup_bounces_received_count")),
        "warmup_bounces_caused": _int_counter(w.get("warmup_bounces_caused_count")),
    }


def _fetch_live_warmup(bison: emailbison.BisonClient) -> dict[int, dict]:
    """Live warmup data per sender id from Bison's /api/warmup/sender-emails."""
    try:
        out: dict[int, dict] = {}
        for row in bison.get_warmup_sender_emails():
            if row.get("id") is None:
                continue
            row = dict(row)
            row["warmup_score"] = _int_score(row.get("warmup_score"))
            out[int(row["id"])] = row
        return out
    except Exception as e:
        logger.warning(f"Failed to fetch live warmup data from Bison: {e}")
        return {}


def _record_warmup_history(supabase, warmup_by_id: dict[int, dict]) -> None:
    """Append today's warmup snapshot per sender (skips senders already
    recorded today so deep-refresh reruns don't duplicate rows)."""
    if not warmup_by_id:
        return
    today_start = datetime.now(timezone.utc).date().isoformat()
    try:
        existing = fetch_all(
            lambda: supabase.table("sender_warmup_history")
            .select("sender_email_id")
            .gte("recorded_at", today_start)
            .order("id")
        )
        already = {r["sender_email_id"] for r in existing}
    except Exception as e:
        logger.warning(f"Failed to read today's warmup history: {e}")
        already = set()

    recorded_at = datetime.now(timezone.utc).isoformat()
    rows = []
    for sid, w in warmup_by_id.items():
        if sid in already:
            continue
        email = w.get("email") or ""
        rows.append({
            "sender_email_id": sid,
            "sender_email": email,
            "domain": w.get("domain") or (email.split("@")[1] if "@" in email else ""),
            "provider": "emailbison",
            "warmup_score": w.get("warmup_score"),
            "recorded_at": recorded_at,
        })
    if not rows:
        return
    try:
        supabase.table("sender_warmup_history").insert(rows).execute()
        logger.info(f"Recorded {len(rows)} warmup history snapshots")
    except Exception as e:
        logger.error(f"Failed to insert warmup history: {e}")


def _fetch_sender_lookup_data(
    supabase,
    sender_ids: list[int],
    live_warmup: dict[int, dict] | None = None,
) -> dict[int, dict]:
    """Batch-fetch warmup scores, recovery status, and deliverability for all senders."""
    if not sender_ids:
        return {}

    lookup: dict[int, dict] = {
        sid: {
            "warmup_score": None,
            "warmup_sent": None,
            "warmup_replied": None,
            "warmup_saved_from_spam": None,
            "warmup_bounces_received": None,
            "warmup_bounces_caused": None,
            "warmup_tags": None,
            "policy_key": None,
            "strike_count": None,
            "next_action_at": None,
            "in_recovery": False,
            "placement_score": None,
            "spam_score": None,
        }
        for sid in sender_ids
    }

    # Warmup score + counters: prefer live Bison data, fall back to history for score.
    for sid, w in (live_warmup or {}).items():
        if sid in lookup:
            lookup[sid]["warmup_score"] = w.get("warmup_score")
            lookup[sid].update(_warmup_counters(w))
            if w.get("tags") is not None:
                lookup[sid]["warmup_tags"] = w.get("tags")

    missing_warmup = [sid for sid in sender_ids if lookup[sid]["warmup_score"] is None]
    if missing_warmup:
        # Latest recorded score per sender (order DESC, take first seen).
        # Bounded to the last 60 days and paged — an unbounded read truncates
        # at the 1000-row cap and silently drops senders.
        warmup_since = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        try:
            rows = fetch_all(
                lambda: supabase.table("sender_warmup_history")
                .select("sender_email_id,warmup_score")
                .in_("sender_email_id", missing_warmup)
                .gte("recorded_at", warmup_since)
                .order("recorded_at", desc=True)
                .order("sender_email_id")
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

    # Latest inbox placement score per sender (last 90 days, paged)
    placement_since = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    try:
        rows = fetch_all(
            lambda: supabase.table("domain_placement_tests")
            .select("sender_email_id,overall_score")
            .in_("sender_email_id", sender_ids)
            .gte("created_at", placement_since)
            .order("created_at", desc=True)
            .order("sender_email_id")
        )
        for row in rows:
            sid = row.get("sender_email_id")
            if sid in lookup and lookup[sid]["placement_score"] is None:
                lookup[sid]["placement_score"] = row.get("overall_score")
    except Exception as e:
        logger.warning(f"Failed to fetch placement scores: {e}")

    # Latest spam filter score per sender (paged)
    try:
        rows = fetch_all(
            lambda: supabase.table("spam_filter_tests")
            .select("sender_email_id,score")
            .in_("sender_email_id", sender_ids)
            .order("created_at", desc=True)
            .order("sender_email_id")
        )
        for row in rows:
            sid = row.get("sender_email_id")
            if sid in lookup and lookup[sid]["spam_score"] is None:
                lookup[sid]["spam_score"] = row.get("score")
    except Exception as e:
        logger.warning(f"Failed to fetch spam scores: {e}")

    return lookup


def _compute_health_score(supabase, reply_rate: float, bounce_rate: float, db: dict) -> int | None:
    """Call compute_sender_health_score RPC. Returns None if RPC is not available.

    Schema override: this RPC lives in `public` (not migrated to v1).
    """
    try:
        result = supabase.schema("public").rpc("compute_sender_health_score", {
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


def poll_sender_email_performance(
    workspace_id: str = DEFAULT_WORKSPACE_ID,
    bison: emailbison.BisonClient | None = None,
) -> None:
    """Fetch email accounts for all active campaigns, deduplicate, and upsert performance rows."""
    supabase = get_supabase()
    today = _today()
    bison = bison or emailbison.for_workspace(workspace_id)
    # Always use the live Bison campaign list — the Supabase document copy
    # goes stale and yields 404s for deleted campaigns (and misses new ones).
    campaign_ids = get_active_campaign_ids_from_bison(bison)
    if not campaign_ids and workspace_id == DEFAULT_WORKSPACE_ID:
        campaign_ids = get_active_campaign_ids(supabase)
    logger.info(f"[{workspace_id}] Polling sender performance across {len(campaign_ids)} campaigns")

    # Collect unique senders across all campaigns
    senders_by_id: dict[int, dict] = {}
    for campaign_id in campaign_ids:
        try:
            accounts = bison.get_campaign_email_accounts(campaign_id)
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

    live_warmup = _fetch_live_warmup(bison)
    _record_warmup_history(supabase, live_warmup)
    lookup = _fetch_sender_lookup_data(supabase, sender_ids, live_warmup)

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

        # Prefer warmup-endpoint tags (set tags); fall back to campaign account tags.
        tags = db.get("warmup_tags")
        if tags is None:
            tags = account.get("tags")

        rows.append({
            "workspace_id": workspace_id,
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
            "warmup_sent": db.get("warmup_sent"),
            "warmup_replied": db.get("warmup_replied"),
            "warmup_saved_from_spam": db.get("warmup_saved_from_spam"),
            "warmup_bounces_received": db.get("warmup_bounces_received"),
            "warmup_bounces_caused": db.get("warmup_bounces_caused"),
            "in_recovery": db.get("in_recovery", False),
            "recovery_policy_key": db.get("policy_key"),
            "recovery_strike_count": db.get("strike_count"),
            "recovery_next_action_at": db.get("next_action_at"),
            "latest_placement_score": db.get("placement_score"),
            "latest_spam_score": db.get("spam_score"),
            "health_score": _compute_health_score(supabase, reply_rate, bounce_rate, db),
            "tags": tags,
            "fetched_at": fetched_at,
        })

    if rows:
        upsert_ok = False
        try:
            supabase.table("sender_email_performance").upsert(
                rows, on_conflict="workspace_id,sender_email_id,snapshot_date"
            ).execute()
            logger.info(f"[{workspace_id}] Batch-upserted {len(rows)} sender performance rows")
            upsert_ok = True
        except Exception as e:
            # Migration 018 may not be applied yet — retry without counter cols.
            msg = str(e).lower()
            counter_keys = (
                "warmup_sent",
                "warmup_replied",
                "warmup_saved_from_spam",
                "warmup_bounces_received",
                "warmup_bounces_caused",
            )
            if any(k in msg for k in counter_keys) or "column" in msg:
                stripped = [
                    {k: v for k, v in r.items() if k not in counter_keys} for r in rows
                ]
                try:
                    supabase.table("sender_email_performance").upsert(
                        stripped,
                        on_conflict="workspace_id,sender_email_id,snapshot_date",
                    ).execute()
                    logger.warning(
                        f"[{workspace_id}] Upserted performance without warmup "
                        f"counters (apply migration 018): {e}"
                    )
                    upsert_ok = True
                except Exception as e2:
                    logger.error(f"Failed to batch-upsert sender performance: {e2}")
            else:
                logger.error(f"Failed to batch-upsert sender performance: {e}")

        if not upsert_ok:
            return

        # Fleet warmup snapshot for GET /warmup/report (historical dates).
        # Keep full in-memory rows (incl. counters) for the JSON payload.
        try:
            persist_warmup_daily_report(
                workspace_id, today, rows=rows, supabase=supabase
            )
        except Exception as e:
            logger.error(f"[{workspace_id}] Failed to persist warmup_daily_report: {e}")


def run() -> None:
    """Main entry point called by the scheduler."""
    logger.info("Starting sender email performance poll")
    for ws in pollable_workspaces():
        try:
            bison = emailbison.for_workspace(ws["id"])
        except Exception as e:
            logger.error(f"Skipping workspace {ws['id']}: {e}")
            continue
        try:
            poll_sender_email_performance(ws["id"], bison)
        except Exception as e:
            logger.error(
                f"sender_performance_poller failed for workspace {ws['id']}: {e}"
            )
    logger.info("Sender email performance poll complete")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
