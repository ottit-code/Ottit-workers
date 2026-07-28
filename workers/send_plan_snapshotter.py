"""
send_plan_snapshotter.py — runs shortly after UTC midnight

Captures the full day's sending plan (EmailBison scheduled-emails queue,
filtered to today) into daily_send_plan before sending starts draining the
queue. /schedule/today reads this snapshot to show remaining vs planned.

Idempotent per day: a workspace already snapshotted today is skipped, so
mid-day restarts don't overwrite the midnight capture with a drained queue.
"""

import logging
from datetime import datetime, timezone

from lib import send_schedule
from lib.config import pollable_workspaces
from lib.supabase_client import get_supabase

logger = logging.getLogger(__name__)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def snapshot_workspace(ws: dict, today: str) -> None:
    supabase = get_supabase()
    existing = (
        supabase.table("daily_send_plan")
        .select("campaign_id", count="exact")
        .eq("workspace_id", ws["id"])
        .eq("plan_date", today)
        .limit(1)
        .execute()
    )
    if existing.count:
        logger.info(f"[{ws['id']}] Plan for {today} already captured — skipping")
        return

    campaigns = send_schedule.plan_for_workspace(ws, today)
    captured_at = datetime.now(timezone.utc).isoformat()
    rows = [
        {
            "workspace_id": ws["id"],
            "plan_date": today,
            "campaign_id": c["campaign_id"],
            "campaign_name": c["campaign_name"],
            "planned": c["planned_today"],
            "inboxes": c["inboxes"],
            "captured_at": captured_at,
        }
        for c in campaigns
        if not c.get("error")
    ]
    if not rows:
        logger.info(f"[{ws['id']}] No active campaigns to snapshot for {today}")
        return
    supabase.table("daily_send_plan").upsert(
        rows, on_conflict="workspace_id,plan_date,campaign_id"
    ).execute()
    total = sum(r["planned"] for r in rows)
    logger.info(
        f"[{ws['id']}] Captured send plan for {today}: "
        f"{total} emails across {len(rows)} campaigns"
    )


def run() -> None:
    """Main entry point called by the scheduler."""
    today = _today()
    logger.info(f"Capturing daily send plan for {today}")
    for ws in pollable_workspaces():
        try:
            snapshot_workspace(ws, today)
        except Exception as e:
            logger.error(f"send_plan_snapshotter failed for workspace {ws['id']}: {e}")
    logger.info("Daily send plan capture complete")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
