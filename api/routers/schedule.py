"""Daily Review — today's sending schedule from EmailBison.

GET /schedule/today reports three numbers for the current UTC day:
- planned:   full day's plan, captured at UTC midnight by send_plan_snapshotter
- sent:      emails actually sent so far today (live Bison workspace stats)
- remaining: what's still queued in Bison's scheduled-emails right now

Read-only: never mutates anything on the Bison side.

Results are cached in-process for ~10 minutes; the manual data refresh
(POST /actions/refresh) clears the cache so the next request re-fetches.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Security

from lib import config, send_schedule
from lib.supabase_client import get_supabase
from api.deps import require_api_key, _cache_get_stale, _cache_set, _cache_revalidate

logger = logging.getLogger(__name__)
router = APIRouter()

_SCHEDULE_CACHE_TTL = 600  # seconds


def _plan_snapshot(today: str, ws_ids: List[str]) -> Dict[str, List[dict]]:
    """Midnight plan snapshot rows grouped by workspace_id."""
    try:
        rows = (
            get_supabase()
            .table("daily_send_plan")
            .select("workspace_id,campaign_id,campaign_name,planned,inboxes")
            .eq("plan_date", today)
            .in_("workspace_id", ws_ids)
            .execute()
            .data or []
        )
        grouped: Dict[str, List[dict]] = {}
        for r in rows:
            grouped.setdefault(r["workspace_id"], []).append(r)
        return grouped
    except Exception as e:
        logger.warning(f"daily_send_plan read failed: {e}")
        return {}


@router.get("/schedule/today", dependencies=[Security(require_api_key)])
def schedule_today(workspace_id: Optional[str] = None, date: Optional[str] = None):
    """Sending schedule for one UTC send day, per campaign and per inbox.

    Defaults to the current UTC day; pass `date` (YYYY-MM-DD) for another day.
    Past/current days use the midnight plan snapshot when available (fast);
    future days page the live Bison queue for items scheduled that day (slow,
    but cached).

    planned_total/planned_today reflect the *current* Bison queue (remaining).
    plan_total is the midnight snapshot of the full day's plan (null until the
    first snapshot exists); sent_total is live sent-so-far from Bison.
    Cached ~10 minutes; the manual data refresh busts the cache.
    """
    utc_today = datetime.now(timezone.utc).date().isoformat()
    today = utc_today
    if date:
        try:
            today = datetime.strptime(date[:10], "%Y-%m-%d").date().isoformat()
        except ValueError:
            pass
    ws_filter = workspace_id if workspace_id and workspace_id != "all" else None
    cache_key = f"schedule_today:{ws_filter or 'all'}:{today}"

    def build() -> Dict[str, Any]:
        # Only token-configured workspaces can be queried on the Bison side.
        pollable = config.pollable_workspaces()
        workspaces = (
            [w for w in pollable if w["id"] == ws_filter]
            if ws_filter else pollable
        )
        snapshot = _plan_snapshot(today, [w["id"] for w in workspaces])

        campaigns: List[Dict[str, Any]] = []
        sent_total: Optional[int] = None
        plan_total: Optional[int] = None
        for ws in workspaces:
            snap_rows = snapshot.get(ws["id"]) or []
            try:
                if snap_rows:
                    # Fast path: remaining derived from the midnight plan and
                    # per-campaign sent-today (seconds, not minutes).
                    fast = send_schedule.plan_from_snapshot(ws, today, snap_rows)
                    for c in fast:
                        c["overdue_today"] = None  # unknown without queue paging
                    campaigns.extend(fast)
                    plan_total = (plan_total or 0) + sum(
                        int(r.get("planned") or 0) for r in snap_rows
                    )
                else:
                    # No snapshot yet: page the live queue (slow but exact).
                    slow = send_schedule.plan_for_workspace(ws, today)
                    for c in slow:
                        c["planned_start"] = None
                        c["sent_today"] = None
                    campaigns.extend(slow)
            except Exception as e:
                logger.warning(f"schedule fetch failed for workspace {ws['id']}: {e}")
            if today > utc_today:
                # Future day: nothing sent yet by definition — skip the lookup.
                sent_total = (sent_total or 0)
            else:
                ws_sent = send_schedule.sent_today_for_workspace(ws["id"], today)
                if ws_sent is not None:
                    sent_total = (sent_total or 0) + ws_sent

        campaigns.sort(key=lambda c: -c["planned_today"])
        remaining_total = sum(c["planned_today"] for c in campaigns)
        overdues = [c.get("overdue_today") for c in campaigns]
        overdue_total = (
            sum(o for o in overdues if o is not None)
            if any(o is not None for o in overdues) else None
        )
        return {
            "date": today,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            # Back-compat: planned_total has always been the live queue.
            "planned_total": remaining_total,
            "remaining_total": remaining_total,
            # Today's queue items whose scheduled slot passed unsent — they
            # roll over to later days, so they're excluded from remaining.
            "overdue_total": overdue_total,
            "plan_total": plan_total,
            "sent_total": sent_total,
            "campaigns": campaigns,
        }

    cached, fresh = _cache_get_stale(cache_key)
    if fresh:
        return cached
    if cached is not None:
        # Building takes ~20s of Bison paging; serve the stale payload now and
        # rebuild in the background. Manual refresh clears the cache entirely,
        # so that path still blocks for genuinely fresh data.
        _cache_revalidate(cache_key, build, _SCHEDULE_CACHE_TTL)
        return cached

    payload = build()
    _cache_set(cache_key, payload, _SCHEDULE_CACHE_TTL)
    return payload
