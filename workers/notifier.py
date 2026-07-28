"""
notifier.py — alert rule engine, runs every 15 minutes

Checks alert conditions and writes to the notifications table.
Supabase Realtime then pushes changes to the dashboard.

Code Red (severity=critical → Slack + bell UI):
- Bundle bounce spike: a tag/bundle's aggregate bounce rate today exceeds
  the threshold with meaningful volume.
- Multi-day reply-rate drop: N consecutive MATURED days (the trailing
  3 still-maturing days are excluded) sit below the cumulative cohort
  baseline by a relative margin. Never fires on a single-day dip.
"""

import logging
from datetime import datetime, timedelta, timezone
from lib.config import WORKSPACES
from lib.supabase_client import get_supabase
from lib.supabase_paginate import fetch_all
from lib.notifications import create_notification

logger = logging.getLogger(__name__)

# --- Code Red thresholds ----------------------------------------------------
BUNDLE_BOUNCE_THRESHOLD = 0.05     # 5% bounce rate per bundle/tag
BUNDLE_MIN_SENT = 100              # ignore bundles with tiny volume today

REPLY_DROP_MATURING_DAYS = 3       # trailing days excluded (replies still landing)
REPLY_DROP_CONSECUTIVE_DAYS = 3    # matured days that must ALL be below baseline
REPLY_DROP_BASELINE_DAYS = 30      # matured days used for the cumulative baseline
REPLY_DROP_RELATIVE_MARGIN = 0.30  # day must be >30% below baseline to count
REPLY_DROP_MIN_SENT_PER_DAY = 50   # skip low-volume days (too noisy)


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


def _normalize_tags(raw) -> list[str]:
    """sender_email_performance.tags is a Bison payload: list of strings or
    list of {"name": ...} dicts. Return clean tag names."""
    if not raw:
        return []
    tags: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            tags.append(item.strip())
        elif isinstance(item, dict) and item.get("name"):
            tags.append(str(item["name"]).strip())
    return tags


def _latest_tags_by_sender() -> dict[tuple, list[str]]:
    """(workspace_id, sender_email_id) → tag names from the latest snapshot.

    Scans the last 14 days paged past the 1000-row cap — the full table
    already exceeds it, which silently dropped senders before.
    """
    since = (datetime.now(timezone.utc).date() - timedelta(days=14)).isoformat()
    try:
        rows = fetch_all(
            lambda: get_supabase()
            .table("sender_email_performance")
            .select("workspace_id,sender_email_id,tags,snapshot_date")
            .gte("snapshot_date", since)
            .order("snapshot_date", desc=True)
            .order("sender_email_id")
        )
    except Exception as e:
        logger.error(f"_latest_tags_by_sender failed: {e}")
        return {}
    latest: dict[tuple, list[str]] = {}
    for row in rows:
        key = (row.get("workspace_id"), str(row.get("sender_email_id")))
        if key not in latest:
            latest[key] = _normalize_tags(row.get("tags"))
    return latest


def check_bundle_bounce_spike(sender_rows: list, notified: set) -> None:
    """Code Red: a bundle/tag's aggregate bounce rate today exceeds threshold."""
    tags_map = _latest_tags_by_sender()
    if not tags_map:
        return

    # Aggregate today's sends/bounces per (workspace, tag).
    agg: dict[tuple, dict] = {}
    for row in sender_rows:
        key = (row.get("workspace_id"), str(row.get("sender_email_id")))
        for tag in tags_map.get(key, []):
            bucket = agg.setdefault((row.get("workspace_id"), tag), {"sent": 0, "bounced": 0})
            bucket["sent"] += row.get("emails_sent", 0) or 0
            bucket["bounced"] += row.get("emails_bounced", 0) or 0

    for (workspace_id, tag), bucket in agg.items():
        sent, bounced = bucket["sent"], bucket["bounced"]
        if sent < BUNDLE_MIN_SENT or bounced / sent <= BUNDLE_BOUNCE_THRESHOLD:
            continue
        entity_id = f"{workspace_id or 'all'}:{tag}"
        if ("bundle_bounce_spike", entity_id) in notified:
            continue
        pct = f"{(bounced / sent * 100):.1f}%"
        create_notification(
            severity="critical",
            type_="bundle_bounce_spike",
            title=f"Code Red — bundle bounce spike: {tag}",
            body=(
                f"Bundle {tag} bounced {bounced}/{sent} sends today ({pct}); "
                f"threshold {BUNDLE_BOUNCE_THRESHOLD:.0%}."
            ),
            entity_type="tag",
            entity_id=entity_id,
        )
        notified.add(("bundle_bounce_spike", entity_id))


def _daily_sent_by_date(workspace_id: str, start: str, end: str) -> dict[str, int]:
    """stat_date → total emails_sent for a workspace (from sender_daily_stats)."""
    rows = fetch_all(
        lambda: get_supabase()
        .table("sender_daily_stats")
        .select("stat_date,emails_sent")
        .eq("workspace_id", workspace_id)
        .gte("stat_date", start)
        .lte("stat_date", end)
        .order("stat_date")
        .order("sender_email_id")
    )
    sent: dict[str, int] = {}
    for row in rows:
        d = str(row.get("stat_date"))
        sent[d] = sent.get(d, 0) + (row.get("emails_sent", 0) or 0)
    return sent


def _cohort_replies_by_date(workspace_id: str, start: str, end: str) -> dict[str, int]:
    """stat_date → cohort replies (attributed to original sent date)."""
    rows = (
        get_supabase()
        .rpc(
            "get_cohort_reply_counts",
            {"p_start": start, "p_end": end, "p_group": "campaign", "p_workspace_id": workspace_id},
        )
        .execute()
        .data or []
    )
    replies: dict[str, int] = {}
    for row in rows:
        d = str(row.get("stat_date"))
        replies[d] = replies.get(d, 0) + int(row.get("cohort_replies") or 0)
    return replies


def check_reply_rate_drop(notified: set) -> None:
    """Code Red: N consecutive matured days below the cumulative cohort baseline.

    The trailing REPLY_DROP_MATURING_DAYS (incl. today) are excluded because
    their replies are still landing. Never fires on a single-day dip.
    """
    today = datetime.now(timezone.utc).date()
    matured_end = today - timedelta(days=REPLY_DROP_MATURING_DAYS)
    recent_start = matured_end - timedelta(days=REPLY_DROP_CONSECUTIVE_DAYS - 1)
    baseline_end = recent_start - timedelta(days=1)
    baseline_start = baseline_end - timedelta(days=REPLY_DROP_BASELINE_DAYS - 1)

    for ws in WORKSPACES:
        workspace_id = ws["id"]
        if ("reply_rate_drop", workspace_id) in notified:
            continue
        try:
            sent = _daily_sent_by_date(workspace_id, baseline_start.isoformat(), matured_end.isoformat())
            replies = _cohort_replies_by_date(workspace_id, baseline_start.isoformat(), matured_end.isoformat())
        except Exception as e:
            # Most common cause: migration 012 (get_cohort_reply_counts RPC)
            # not applied yet — the check is a no-op until then.
            logger.warning(f"check_reply_rate_drop skipped for {workspace_id}: {e}")
            continue

        # Cumulative baseline over the matured window before the recent days.
        base_sent = base_replies = 0
        day = baseline_start
        while day <= baseline_end:
            key = day.isoformat()
            base_sent += sent.get(key, 0)
            base_replies += replies.get(key, 0)
            day += timedelta(days=1)
        if base_sent < REPLY_DROP_MIN_SENT_PER_DAY * REPLY_DROP_CONSECUTIVE_DAYS:
            continue  # not enough history for a meaningful baseline
        baseline_rate = base_replies / base_sent if base_sent else 0.0
        if baseline_rate <= 0:
            continue

        # Every one of the recent matured days must sit below the baseline
        # by the relative margin (with enough volume to be meaningful).
        cutoff = baseline_rate * (1 - REPLY_DROP_RELATIVE_MARGIN)
        recent: list[tuple[str, float]] = []
        all_below = True
        day = recent_start
        while day <= matured_end:
            key = day.isoformat()
            day_sent = sent.get(key, 0)
            if day_sent < REPLY_DROP_MIN_SENT_PER_DAY:
                all_below = False
                break
            day_rate = replies.get(key, 0) / day_sent
            recent.append((key, day_rate))
            if day_rate >= cutoff:
                all_below = False
                break
            day += timedelta(days=1)

        if not all_below or len(recent) < REPLY_DROP_CONSECUTIVE_DAYS:
            continue

        detail = ", ".join(f"{d}: {r * 100:.2f}%" for d, r in recent)
        create_notification(
            severity="critical",
            type_="reply_rate_drop",
            title=f"Code Red — reply rate dropping ({ws.get('name', workspace_id)})",
            body=(
                f"{REPLY_DROP_CONSECUTIVE_DAYS} consecutive matured days below the "
                f"{baseline_rate * 100:.2f}% cohort baseline by >{REPLY_DROP_RELATIVE_MARGIN:.0%} "
                f"({detail}). Trailing {REPLY_DROP_MATURING_DAYS} still-maturing days excluded."
            ),
            entity_type="workspace",
            entity_id=workspace_id,
        )
        notified.add(("reply_rate_drop", workspace_id))


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
        sender_rows = fetch_all(
            lambda: supabase.table("sender_daily_stats")
            .select("workspace_id,sender_email_id,sender_email,emails_sent,emails_bounced,daily_limit")
            .eq("stat_date", _today())
            .order("sender_email_id")
        )
    except Exception as e:
        logger.error(f"notifier: failed to load sender_daily_stats: {e}")
        sender_rows = []

    # Load today's notifications once for in-memory dedup
    notified = _load_todays_notifications()

    for fn, args in [
        (check_bounce_rate_spike, (sender_rows, notified)),
        (check_bundle_bounce_spike, (sender_rows, notified)),
        (check_daily_limit_approaching, (sender_rows, notified)),
        (check_reply_rate_drop, (notified,)),
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
