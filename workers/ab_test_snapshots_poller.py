"""
ab_test_snapshots_poller.py — runs every 6 hours

Fetches A/B test data from EmailBison and upserts into Supabase:
- ab_test_snapshots: per-step engagement stats with optional statistical significance
"""

import logging
from collections import defaultdict
from datetime import datetime, timezone

from lib import emailbison
from lib.supabase_client import get_supabase
from lib.utils import get_active_campaign_ids

logger = logging.getLogger(__name__)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _safe_rate(numerator: int, denominator: int) -> float:
    """Return percentage rate, or 0.0 when denominator is zero."""
    if not denominator:
        return 0.0
    return round((numerator / denominator) * 100, 4)


def _aggregate_scheduled_emails(scheduled_emails: list) -> dict[str, dict]:
    """Aggregate per-email stats keyed by sequence_step_id string."""
    agg: dict[str, dict] = defaultdict(lambda: {
        "emails_sent": 0,
        "opens": 0,
        "unique_opens": 0,
        "clicks": 0,
        "replies": 0,
        "unique_replies": 0,
        "interested": 0,
        "bounced": 0,
    })
    for email in scheduled_emails:
        sid = str(email.get("sequence_step_id") or "")
        if not sid:
            continue
        a = agg[sid]
        status = (email.get("status") or "").lower()
        a["emails_sent"] += 1 if status == "sent" else 0
        a["opens"] += int(email.get("opens") or 0)
        a["unique_opens"] += int(email.get("unique_opens") or 0)
        a["clicks"] += int(email.get("clicks") or 0)
        a["replies"] += int(email.get("replies") or 0)
        a["unique_replies"] += int(email.get("unique_replies") or 0)
        a["interested"] += 1 if email.get("interested") else 0
        a["bounced"] += 1 if status == "bounced" else 0
    return dict(agg)


def _empty_agg() -> dict:
    return {
        "emails_sent": 0, "opens": 0, "unique_opens": 0,
        "clicks": 0, "replies": 0, "unique_replies": 0,
        "interested": 0, "bounced": 0,
    }


def _compute_significance(supabase, parent_a: dict, variant_a: dict) -> tuple:
    """Call compute_ab_significance RPC. Returns (confidence, winner, sample_sufficient)."""
    try:
        result = supabase.rpc("compute_ab_significance", {
            "control_replies": parent_a.get("unique_replies", 0),
            "control_sent": parent_a.get("emails_sent", 0),
            "variant_replies": variant_a.get("unique_replies", 0),
            "variant_sent": variant_a.get("emails_sent", 0),
            "min_sample": 30,
        }).execute()
        if result.data:
            sig = result.data if isinstance(result.data, dict) else result.data[0]
            return (
                sig.get("stat_confidence"),
                sig.get("stat_winner"),
                sig.get("stat_sample_sufficient"),
            )
    except Exception as e:
        logger.warning(f"compute_ab_significance RPC unavailable: {e}")
    return None, None, None


def poll_ab_test_snapshots() -> None:
    """Fetch sequence step stats for all active campaigns and upsert into ab_test_snapshots."""
    supabase = get_supabase()
    today = _today()
    campaign_ids = get_active_campaign_ids(supabase)
    logger.info(f"Polling A/B test snapshots for {len(campaign_ids)} campaigns")

    all_rows: list[dict] = []

    for campaign_id in campaign_ids:
        try:
            steps = emailbison.get_campaign_sequence_steps(campaign_id)
            scheduled_emails = emailbison.get_campaign_scheduled_emails(campaign_id)
            agg = _aggregate_scheduled_emails(scheduled_emails)

            fetched_at = datetime.now(timezone.utc).isoformat()
            for step in steps:
                step_id = str(step.get("id") or "")
                if not step_id:
                    continue

                a = agg.get(step_id, _empty_agg())
                sent = a["emails_sent"]

                # Statistical significance — only for variant steps
                stat_confidence = stat_winner = stat_sample_sufficient = None
                parent_step_id = step.get("variant_from_step_id")
                if parent_step_id:
                    parent_a = agg.get(str(parent_step_id), _empty_agg())
                    stat_confidence, stat_winner, stat_sample_sufficient = (
                        _compute_significance(supabase, parent_a, a)
                    )

                all_rows.append({
                    "campaign_id": str(campaign_id),
                    "sequence_step_id": int(step_id),
                    "snapshot_date": today,
                    "email_subject": step.get("email_subject"),
                    "step_order": step.get("order"),
                    "is_variant": bool(step.get("variant")),
                    "variant_from_step_id": int(parent_step_id) if parent_step_id else None,
                    "thread_reply": bool(step.get("thread_reply", False)),
                    "emails_sent": sent,
                    "opens": a["opens"],
                    "unique_opens": a["unique_opens"],
                    "clicks": a["clicks"],
                    "replies": a["replies"],
                    "unique_replies": a["unique_replies"],
                    "interested": a["interested"],
                    "bounced": a["bounced"],
                    "open_rate": _safe_rate(a["unique_opens"], sent),
                    "reply_rate": _safe_rate(a["unique_replies"], sent),
                    "click_rate": _safe_rate(a["clicks"], sent),
                    "interest_rate": _safe_rate(a["interested"], sent),
                    "bounce_rate": _safe_rate(a["bounced"], sent),
                    "stat_confidence": stat_confidence,
                    "stat_winner": stat_winner,
                    "stat_sample_sufficient": stat_sample_sufficient,
                    "fetched_at": fetched_at,
                })
        except Exception as e:
            logger.error(f"Failed to process A/B snapshots for campaign {campaign_id}: {e}")

    if all_rows:
        try:
            supabase.table("ab_test_snapshots").upsert(
                all_rows, on_conflict="sequence_step_id,snapshot_date"
            ).execute()
            logger.info(f"Batch-upserted {len(all_rows)} A/B test snapshot rows")
        except Exception as e:
            logger.error(f"Failed to batch-upsert A/B test snapshots: {e}")


def run() -> None:
    """Main entry point called by the scheduler."""
    logger.info("Starting A/B test snapshots poll")
    try:
        poll_ab_test_snapshots()
    except Exception as e:
        logger.error(f"ab_test_snapshots_poller.poll_ab_test_snapshots failed: {e}")
    logger.info("A/B test snapshots poll complete")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
