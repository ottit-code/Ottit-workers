"""
send_plan_snapshotter.py — captures send plans into daily_send_plan

Two entry points:
- run():          shortly after UTC midnight — captures the full day's plan
                  (EmailBison scheduled-emails queue, filtered to today)
                  before sending starts draining the queue.
- run_tomorrow(): every deep refresh (3-hourly) — pre-captures *tomorrow's*
                  queued plan so /schedule/today?date=<tomorrow> serves from
                  Supabase instead of paging Bison's queue live (minutes).

Midnight runs are idempotent per day: a capture made at/after the day
started is never overwritten (mid-day restarts would record a drained
queue). Pre-captures from the previous day ARE replaced by the canonical
midnight run.
"""

import logging
from datetime import datetime, timedelta, timezone

from lib import send_schedule
from lib.config import pollable_workspaces
from lib.supabase_client import get_supabase

logger = logging.getLogger(__name__)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def snapshot_workspace(ws: dict, day: str, replace: bool = False) -> None:
    supabase = get_supabase()
    if not replace:
        existing = (
            supabase.table("daily_send_plan")
            .select("captured_at")
            .eq("workspace_id", ws["id"])
            .eq("plan_date", day)
            .order("captured_at", desc=True)
            .limit(1)
            .execute()
            .data
            or []
        )
        # Skip only if captured at/after the day started (the canonical
        # midnight run). Pre-captures from the day before get replaced.
        if existing and str(existing[0].get("captured_at") or "") >= f"{day}T00:00:00":
            logger.info(f"[{ws['id']}] Plan for {day} already captured — skipping")
            return

    campaigns = send_schedule.plan_for_workspace(ws, day)
    ok = [c for c in campaigns if not c.get("error")]
    if campaigns and not ok:
        # Bison fetch failed across the board — keep whatever capture exists.
        logger.warning(f"[{ws['id']}] All campaign fetches failed for {day} — keeping previous capture")
        return

    captured_at = datetime.now(timezone.utc).isoformat()
    rows = [
        {
            "workspace_id": ws["id"],
            "plan_date": day,
            "campaign_id": c["campaign_id"],
            "campaign_name": c["campaign_name"],
            "planned": c["planned_today"],
            "inboxes": c["inboxes"],
            "captured_at": captured_at,
        }
        for c in ok
    ]
    # Replace the previous capture wholesale so campaigns that left the
    # queue don't linger with stale counts.
    supabase.table("daily_send_plan").delete().eq("workspace_id", ws["id"]).eq(
        "plan_date", day
    ).execute()
    if not rows:
        logger.info(f"[{ws['id']}] No queued sends to snapshot for {day}")
        return
    supabase.table("daily_send_plan").insert(rows).execute()
    total = sum(r["planned"] for r in rows)
    logger.info(
        f"[{ws['id']}] Captured send plan for {day}: "
        f"{total} emails across {len(rows)} campaigns"
    )


def run() -> None:
    """Midnight capture of today's full plan (called by the scheduler)."""
    today = _today()
    logger.info(f"Capturing daily send plan for {today}")
    for ws in pollable_workspaces():
        try:
            snapshot_workspace(ws, today)
        except Exception as e:
            logger.error(f"send_plan_snapshotter failed for workspace {ws['id']}: {e}")
    logger.info("Daily send plan capture complete")


def run_tomorrow() -> None:
    """Pre-capture tomorrow's queued plan (called by deep_refresh every 3h)."""
    day = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    logger.info(f"Pre-capturing send plan for {day}")
    for ws in pollable_workspaces():
        try:
            snapshot_workspace(ws, day, replace=True)
        except Exception as e:
            logger.error(f"tomorrow send-plan capture failed for workspace {ws['id']}: {e}")
    logger.info(f"Tomorrow send plan capture complete ({day})")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
