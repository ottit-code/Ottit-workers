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


def _prior_cumulative_map(supabase, today: str) -> dict:
    """Latest cumulative snapshot per (workspace, sender) before today.

    sender_daily_stats stores Bison's *lifetime* counters per snapshot day, so
    "today's" activity is the delta from the most recent prior snapshot.
    Bounded to the last 7 days (snapshots land at least daily).
    """
    since = (
        datetime.now(timezone.utc) - timedelta(days=7)
    ).date().isoformat()
    rows = fetch_all(
        lambda: supabase.table("sender_daily_stats")
        .select("workspace_id,sender_email_id,stat_date,emails_sent,emails_bounced")
        .gte("stat_date", since)
        .lt("stat_date", today)
        .order("stat_date", desc=True)
        .order("sender_email_id")
    )
    prior: dict = {}
    for r in rows:
        key = (r.get("workspace_id"), r.get("sender_email_id"))
        if key not in prior:  # rows are newest-first
            prior[key] = r
    return prior


def _with_daily_deltas(sender_rows: list, prior: dict) -> list:
    """Attach sent_today / bounced_today deltas to each of today's rows.

    Senders without a prior snapshot get None deltas — their cumulative
    counters can't be attributed to today, so delta-based checks skip them.
    """
    out = []
    for row in sender_rows:
        key = (row.get("workspace_id"), row.get("sender_email_id"))
        prev = prior.get(key)
        row = dict(row)
        if prev is None:
            row["sent_today"] = None
            row["bounced_today"] = None
        else:
            row["sent_today"] = max(
                (row.get("emails_sent") or 0) - (prev.get("emails_sent") or 0), 0
            )
            row["bounced_today"] = max(
                (row.get("emails_bounced") or 0) - (prev.get("emails_bounced") or 0), 0
            )
        out.append(row)
    return out


def check_bounce_rate_spike(sender_rows: list, notified: set) -> None:
    """Alert if any sender's bounce rate *today* exceeds 5%.

    Uses daily deltas — the raw counters are cumulative lifetime values.
    Requires a minimum of 20 sends today to avoid noise on tiny volumes.
    """
    for row in sender_rows:
        sent = row.get("sent_today")
        bounced = row.get("bounced_today")
        if sent is None or bounced is None:
            continue
        if sent >= 20 and bounced / sent > 0.05:
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
    """One summary per workspace when senders hit 90%+ of their daily limit.

    Hitting the limit is normal for a fleet at fixed caps (warmup senders at
    10/day reach it every day) — a per-sender alert produced hundreds of
    notifications daily and drowned the activity feed, so this rolls them up.
    Delta-based: raw emails_sent counters are cumulative lifetime, not per-day.
    """
    at_limit: dict[str, list] = {}
    for row in sender_rows:
        limit = row.get("daily_limit", 0) or 0
        sent = row.get("sent_today")
        if sent is None or limit <= 0:
            continue
        if sent / limit >= 0.9:
            at_limit.setdefault(row.get("workspace_id") or "unknown", []).append(row)

    for workspace_id, rows in at_limit.items():
        entity_id = workspace_id
        if ("daily_limit_approaching", entity_id) in notified:
            continue
        sample = ", ".join(
            str(r.get("sender_email") or r.get("sender_email_id")) for r in rows[:3]
        )
        more = f" and {len(rows) - 3} more" if len(rows) > 3 else ""
        create_notification(
            severity="info",
            type_="daily_limit_approaching",
            title=f"{len(rows)} senders at/near daily limit ({workspace_id})",
            body=f"Used 90%+ of their daily send limit today: {sample}{more}.",
            entity_type="workspace",
            entity_id=entity_id,
        )
        notified.add(("daily_limit_approaching", entity_id))


def _normalize_tags(raw) -> list[str]:
    """sender_email_performance.tags is a Bison payload: list of strings or
    list of {"name": ...} dicts. Return clean tag names.

    Bison mirrors each bundle tag with an internal "p."-prefixed copy
    (p.CI-DED-Set4-0518, …) — excluded as a rule so bundles aren't
    double-counted in alerts.
    """
    if not raw:
        return []
    tags: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            tags.append(item.strip())
        elif isinstance(item, dict) and item.get("name"):
            tags.append(str(item["name"]).strip())
    return [t for t in tags if "p." not in t]


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

    # Aggregate today's sends/bounces per (workspace, tag) — daily deltas,
    # not the raw cumulative counters.
    agg: dict[tuple, dict] = {}
    for row in sender_rows:
        if row.get("sent_today") is None:
            continue
        key = (row.get("workspace_id"), str(row.get("sender_email_id")))
        for tag in tags_map.get(key, []):
            bucket = agg.setdefault((row.get("workspace_id"), tag), {"sent": 0, "bounced": 0})
            bucket["sent"] += row.get("sent_today") or 0
            bucket["bounced"] += row.get("bounced_today") or 0

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
    """stat_date → emails actually sent that day for a workspace.

    sender_daily_stats stores cumulative lifetime counters per snapshot, so
    the per-day figure is each sender's delta from its previous snapshot.
    Fetches a few extra days before `start` to seed the first deltas.
    """
    seed_start = (
        datetime.fromisoformat(start).date() - timedelta(days=7)
    ).isoformat()
    rows = fetch_all(
        lambda: get_supabase()
        .table("sender_daily_stats")
        .select("sender_email_id,stat_date,emails_sent")
        .eq("workspace_id", workspace_id)
        .gte("stat_date", seed_start)
        .lte("stat_date", end)
        .order("sender_email_id")
        .order("stat_date")
    )
    sent: dict[str, int] = {}
    prev_by_sender: dict = {}
    for row in rows:  # ordered by sender, then date
        sid = row.get("sender_email_id")
        d = str(row.get("stat_date"))
        cum = row.get("emails_sent", 0) or 0
        prev = prev_by_sender.get(sid)
        prev_by_sender[sid] = cum
        if prev is None or d < start:
            continue  # no baseline yet, or still in the seed window
        delta = max(cum - prev, 0)
        sent[d] = sent.get(d, 0) + delta
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

    # Load sender stats once — shared across both sender checks. Counters are
    # cumulative lifetime values, so attach per-day deltas before checking.
    supabase = get_supabase()
    try:
        today = _today()
        raw_rows = fetch_all(
            lambda: supabase.table("sender_daily_stats")
            .select("workspace_id,sender_email_id,sender_email,emails_sent,emails_bounced,daily_limit")
            .eq("stat_date", today)
            .order("sender_email_id")
        )
        sender_rows = _with_daily_deltas(raw_rows, _prior_cumulative_map(supabase, today))
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
