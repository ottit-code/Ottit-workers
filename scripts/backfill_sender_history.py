"""
backfill_sender_history.py — one-off historical backfill for sender_daily_stats

sender_daily_stats only has cumulative snapshots from the days the stats
poller actually ran (real coverage starts 2026-07-27 for most senders), so
every dashboard range (7d/30d/90d/1y) sums the same couple of days.

Bison's GET /api/campaign-events/stats returns true per-day Sent / Replied /
Bounced / Opens series and accepts a sender_email_ids filter, so history IS
available — one call per sender. This script reconstructs each sender's
cumulative series backwards from its earliest existing snapshot (the anchor):

    cum(day) = cum(day+1) - events(day+1)        (clamped >= 0)

and inserts one row per day from the sender's first recorded activity up to
the day before the anchor. The delta-based RPCs (migration 016) then produce
exact per-day numbers for the whole backfilled window.

Warmup counters have no per-day history in Bison, so backfilled rows carry
the anchor's warmup values (flat -> 0 deltas), same for daily_limit.

Idempotent: rows are upserted on (workspace_id, sender_email_id, stat_date)
and only days before each sender's earliest snapshot are written.

Usage:
    python3 -m scripts.backfill_sender_history [--days 180] [--workspace ws_v2]
"""

import argparse
import logging
import time
from datetime import date, datetime, timedelta, timezone

from lib import emailbison
from lib.config import pollable_workspaces
from lib.supabase_client import get_supabase

logger = logging.getLogger(__name__)

METRICS = {
    "Sent": "emails_sent",
    "Replied": "emails_replied",
    "Bounced": "emails_bounced",
    "Total Opens": "emails_opened",
}


def _anchors(workspace_id: str) -> dict[int, dict]:
    """Earliest snapshot row per sender (the cumulative anchor)."""
    sb = get_supabase()
    anchors: dict[int, dict] = {}
    page, size = 0, 1000
    while True:
        rows = (
            sb.table("sender_daily_stats")
            .select(
                "sender_email_id,sender_email,domain,stat_date,emails_sent,"
                "emails_replied,emails_bounced,emails_opened,warmup_sent,"
                "warmup_replied,daily_limit,warmup_enabled"
            )
            .eq("workspace_id", workspace_id)
            .order("stat_date")
            .order("sender_email_id")
            .range(page * size, (page + 1) * size - 1)
            .execute()
            .data
            or []
        )
        for r in rows:
            sid = int(r["sender_email_id"])
            if sid not in anchors:  # first (earliest) row wins
                anchors[sid] = r
        if len(rows) < size:
            return anchors
        page += 1


def _daily_events(bison, sender_id: int, start: str, end: str) -> dict[str, dict[str, int]]:
    """{metric_column: {day: count}} for one sender over [start, end]."""
    resp = bison.get(
        "/api/campaign-events/stats",
        params={"start_date": start, "end_date": end, "sender_email_ids[]": [sender_id]},
    )
    out: dict[str, dict[str, int]] = {col: {} for col in METRICS.values()}
    for series in (resp or {}).get("data", []):
        col = METRICS.get(series.get("label"))
        if not col:
            continue
        for day, count in series.get("dates", []):
            if count:
                out[col][day] = int(count)
    return out


def backfill_sender(bison, workspace_id: str, anchor: dict, days: int) -> list[dict]:
    """Rows to insert for one sender (may be empty)."""
    sid = int(anchor["sender_email_id"])
    anchor_day = date.fromisoformat(anchor["stat_date"])
    window_start = anchor_day - timedelta(days=days)

    events = _daily_events(
        bison, sid, window_start.isoformat(), anchor_day.isoformat()
    )
    active_days = sorted({d for per_day in events.values() for d in per_day})
    # Only pre-anchor activity needs rows.
    active_days = [d for d in active_days if d < anchor_day.isoformat()]
    if not active_days:
        return []

    first_active = date.fromisoformat(active_days[0])
    fetched_at = datetime.now(timezone.utc).isoformat()

    # Walk backwards from the anchor, subtracting each day's events.
    cum = {col: int(anchor.get(col) or 0) for col in METRICS.values()}
    rows: list[dict] = []
    day = anchor_day - timedelta(days=1)
    while day >= first_active:
        next_day = (day + timedelta(days=1)).isoformat()
        for col in cum:
            cum[col] = max(0, cum[col] - events[col].get(next_day, 0))
        rows.append({
            "workspace_id": workspace_id,
            "sender_email_id": sid,
            "sender_email": anchor.get("sender_email") or "",
            "domain": anchor.get("domain") or "",
            "stat_date": day.isoformat(),
            "emails_sent": cum["emails_sent"],
            "emails_opened": cum["emails_opened"],
            "emails_replied": cum["emails_replied"],
            "emails_bounced": cum["emails_bounced"],
            # No per-day warmup history in Bison — flat anchor values give
            # 0-deltas instead of a fake spike on the anchor day.
            "warmup_sent": int(anchor.get("warmup_sent") or 0),
            "warmup_replied": int(anchor.get("warmup_replied") or 0),
            "daily_limit": int(anchor.get("daily_limit") or 0),
            "warmup_enabled": bool(anchor.get("warmup_enabled")),
            "fetched_at": fetched_at,
        })
        day -= timedelta(days=1)
    return rows


def run(days: int, only_workspace: str | None = None) -> None:
    sb = get_supabase()
    for ws in pollable_workspaces():
        if only_workspace and ws["id"] != only_workspace:
            continue
        bison = emailbison.for_workspace(ws["id"])
        anchors = _anchors(ws["id"])
        # Senders whose anchor counters are all 0 never sent before the
        # anchor — nothing to backfill, skip the API call.
        candidates = {
            sid: a
            for sid, a in anchors.items()
            if any(int(a.get(col) or 0) > 0 for col in METRICS.values())
        }
        logger.info(
            f"[{ws['id']}] {len(anchors)} senders, {len(candidates)} with pre-anchor history"
        )

        pending: list[dict] = []
        done = 0
        for sid, anchor in candidates.items():
            try:
                pending.extend(backfill_sender(bison, ws["id"], anchor, days))
            except Exception as e:
                logger.warning(f"[{ws['id']}] sender {sid} backfill failed: {e}")
            done += 1
            if done % 50 == 0:
                logger.info(f"[{ws['id']}] {done}/{len(candidates)} senders processed")
            if len(pending) >= 500:
                sb.table("sender_daily_stats").upsert(
                    pending, on_conflict="workspace_id,sender_email_id,stat_date"
                ).execute()
                pending = []
            time.sleep(0.1)  # gentle on Bison

        if pending:
            sb.table("sender_daily_stats").upsert(
                pending, on_conflict="workspace_id,sender_email_id,stat_date"
            ).execute()
        logger.info(f"[{ws['id']}] Backfill complete")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=180, help="max lookback before each sender's first snapshot")
    parser.add_argument("--workspace", default=None, help="restrict to one workspace id")
    args = parser.parse_args()
    run(args.days, args.workspace)
