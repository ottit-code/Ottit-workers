"""Daily Review — today's sending schedule from EmailBison.

GET /schedule/today reports three numbers for the current UTC day:
- planned:   full day's plan, captured at UTC midnight by send_plan_snapshotter
- sent:      emails actually sent so far today (live Bison workspace stats)
- remaining: what's still queued in Bison's scheduled-emails right now
             (future-scheduled only — overdue items are excluded)

Read-only: never mutates anything on the Bison side.

Each workspace is cached independently (~10 min). "All workspaces" is the
sum of those same per-workspace entries, so all == v1 + v2.

For the current/past day with a midnight snapshot, the first response is a
fast planned-sent estimate (seconds). A background job then pages the live
Bison queue and replaces the cache with exact remaining + overdue. Future
days with a pre-capture serve entirely from Supabase (instant).
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
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
            .select("workspace_id,campaign_id,campaign_name,planned,inboxes,captured_at")
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


def _finalize(
    campaigns: List[Dict[str, Any]],
    *,
    today: str,
    plan_total: Optional[int],
    sent_total: Optional[int],
    generated_at: Optional[str],
    approximate: bool = False,
) -> Dict[str, Any]:
    campaigns = sorted(campaigns, key=lambda c: -c["planned_today"])
    remaining_total = sum(c["planned_today"] for c in campaigns)
    overdues = [c.get("overdue_today") for c in campaigns]
    overdue_total = (
        sum(o for o in overdues if o is not None)
        if any(o is not None for o in overdues) else None
    )
    return {
        "date": today,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "planned_total": remaining_total,
        "remaining_total": remaining_total,
        "overdue_total": overdue_total,
        "plan_total": plan_total,
        "sent_total": sent_total,
        "approximate": approximate,
        "campaigns": campaigns,
    }


def _merge_live_with_snapshot(
    ws: dict, live: List[Dict[str, Any]], snap_rows: List[dict]
) -> tuple[List[Dict[str, Any]], int, Optional[str]]:
    """Overlay live remaining/overdue onto the midnight plan snapshot.

    Remaining always comes from the live Bison queue (future-scheduled only).
    planned_start / plan_total come from the midnight snapshot. Campaigns that
    drained after midnight stay visible with remaining=0.
    """
    snap_by_cid = {str(r["campaign_id"]): r for r in snap_rows}
    live_by_cid = {str(c["campaign_id"]): c for c in live}
    campaigns: List[Dict[str, Any]] = []
    snapshot_at: Optional[str] = None

    for r in snap_rows:
        cid = str(r["campaign_id"])
        planned = int(r.get("planned") or 0)
        live_c = live_by_cid.get(cid)
        cap = r.get("captured_at")
        if cap and (snapshot_at is None or cap > snapshot_at):
            snapshot_at = cap
        if live_c:
            campaigns.append({
                **live_c,
                "planned_start": planned,
                "sent_today": live_c.get("sent_today"),
            })
        else:
            campaigns.append({
                "workspace_id": ws["id"],
                "workspace_name": ws["name"],
                "campaign_id": cid,
                "campaign_name": r.get("campaign_name") or "",
                "planned_today": 0,
                "planned_start": planned,
                "sent_today": None,
                "overdue_today": 0,
                "inboxes": [],
                "error": None,
            })

    # Campaigns scheduled after the midnight snapshot.
    for cid, live_c in live_by_cid.items():
        if cid not in snap_by_cid:
            campaigns.append({
                **live_c,
                "planned_start": None,
                "sent_today": live_c.get("sent_today"),
            })

    plan_total = sum(int(r.get("planned") or 0) for r in snap_rows)
    return campaigns, plan_total, snapshot_at


def _build_fast_from_snapshot(
    ws: dict, today: str, snap_rows: List[dict]
) -> Dict[str, Any]:
    """Seconds-fast estimate: remaining ≈ planned - sent (may include overdue)."""
    campaigns = send_schedule.plan_from_snapshot(ws, today, snap_rows)
    plan_total = sum(int(r.get("planned") or 0) for r in snap_rows)
    snapshot_at = None
    for r in snap_rows:
        cap = r.get("captured_at")
        if cap and (snapshot_at is None or cap > snapshot_at):
            snapshot_at = cap
    sent_total = send_schedule.sent_today_for_workspace(ws["id"], today)
    return _finalize(
        campaigns,
        today=today,
        plan_total=plan_total,
        sent_total=sent_total,
        generated_at=snapshot_at,
        approximate=True,
    )


def _build_future_from_snapshot(
    ws: dict, today: str, snap_rows: List[dict]
) -> Dict[str, Any]:
    """Future day pre-capture — remaining == planned, no Bison calls."""
    campaigns: List[Dict[str, Any]] = []
    snapshot_at: Optional[str] = None
    for r in snap_rows:
        planned = int(r.get("planned") or 0)
        campaigns.append({
            "workspace_id": ws["id"],
            "workspace_name": ws["name"],
            "campaign_id": str(r["campaign_id"]),
            "campaign_name": r.get("campaign_name") or "",
            "planned_today": planned,
            "planned_start": planned,
            "sent_today": None,
            "overdue_today": None,
            "inboxes": r.get("inboxes") or [],
            "error": None,
        })
        cap = r.get("captured_at")
        if cap and (snapshot_at is None or cap > snapshot_at):
            snapshot_at = cap
    plan_total = sum(int(r.get("planned") or 0) for r in snap_rows)
    return _finalize(
        campaigns,
        today=today,
        plan_total=plan_total,
        sent_total=0,
        generated_at=snapshot_at,
        approximate=False,
    )


def _build_live(
    ws: dict, today: str, utc_today: str, snap_rows: List[dict]
) -> Dict[str, Any]:
    """Exact remaining from the live Bison queue (slow; excludes overdue)."""
    campaigns: List[Dict[str, Any]] = []
    plan_total: Optional[int] = None
    snapshot_at: Optional[str] = None

    try:
        if snap_rows and today > utc_today:
            return _build_future_from_snapshot(ws, today, snap_rows)
        if snap_rows:
            live = send_schedule.plan_for_workspace(ws, today)
            for c in live:
                c["sent_today"] = None
            campaigns, plan_total, snapshot_at = _merge_live_with_snapshot(
                ws, live, snap_rows
            )
        else:
            live = send_schedule.plan_for_workspace(ws, today)
            for c in live:
                c["planned_start"] = None
                c["sent_today"] = None
            campaigns = live
    except Exception as e:
        logger.warning(f"live schedule fetch failed for workspace {ws['id']}: {e}")
        if snap_rows:
            return _build_fast_from_snapshot(ws, today, snap_rows)

    if today > utc_today:
        sent_total: Optional[int] = 0
    else:
        sent_total = send_schedule.sent_today_for_workspace(ws["id"], today)

    return _finalize(
        campaigns,
        today=today,
        plan_total=plan_total,
        sent_total=sent_total,
        generated_at=snapshot_at,
        approximate=False,
    )


def _merge_workspaces(parts: List[Dict[str, Any]], today: str) -> Dict[str, Any]:
    """Combine per-workspace payloads so all == sum(parts)."""
    campaigns: List[Dict[str, Any]] = []
    for part in parts:
        campaigns.extend(part.get("campaigns") or [])

    sent_vals = [p.get("sent_total") for p in parts]
    sent_total: Optional[int] = (
        sum(s for s in sent_vals if s is not None)
        if any(s is not None for s in sent_vals) else None
    )

    plan_vals = [p.get("plan_total") for p in parts]
    plan_total: Optional[int] = (
        sum(p for p in plan_vals if p is not None)
        if any(p is not None for p in plan_vals) else None
    )

    generated_ats = [p.get("generated_at") for p in parts if p.get("generated_at")]
    generated_at = max(generated_ats) if generated_ats else datetime.now(timezone.utc).isoformat()
    approximate = any(p.get("approximate") for p in parts)

    merged = _finalize(
        campaigns,
        today=today,
        plan_total=plan_total,
        sent_total=sent_total,
        generated_at=generated_at,
        approximate=approximate,
    )
    # Prefer summed per-workspace overdue totals — approximate payloads leave
    # campaign.overdue_today null even when the workspace total is known later.
    overdue_vals = [p.get("overdue_total") for p in parts]
    if any(o is not None for o in overdue_vals):
        merged["overdue_total"] = sum(o for o in overdue_vals if o is not None)
    return merged


def _get_workspace_schedule(
    ws: dict, today: str, utc_today: str, snap_rows: List[dict]
) -> Dict[str, Any]:
    """Cached per-workspace schedule (stale-while-revalidate).

    Cold cache for a snapshotted current/past day returns the fast estimate
    immediately and rebuilds exact remaining in the background — so Daily
    Review never blocks on minutes of Bison queue paging.
    """
    cache_key = f"schedule_today:{ws['id']}:{today}"

    def build_live() -> Dict[str, Any]:
        snapshot = _plan_snapshot(today, [ws["id"]])
        return _build_live(ws, today, utc_today, snapshot.get(ws["id"]) or [])

    cached, fresh = _cache_get_stale(cache_key)
    if fresh:
        # Approximate entries should still be upgraded to live in the background.
        if cached.get("approximate"):
            _cache_revalidate(cache_key, build_live, _SCHEDULE_CACHE_TTL)
        return cached
    if cached is not None:
        _cache_revalidate(cache_key, build_live, _SCHEDULE_CACHE_TTL)
        return cached

    # Cold cache.
    if snap_rows and today > utc_today:
        payload = _build_future_from_snapshot(ws, today, snap_rows)
        _cache_set(cache_key, payload, _SCHEDULE_CACHE_TTL)
        return payload

    if snap_rows:
        # Instant response, then exact live remaining in the background.
        payload = _build_fast_from_snapshot(ws, today, snap_rows)
        _cache_set(cache_key, payload, _SCHEDULE_CACHE_TTL)
        _cache_revalidate(cache_key, build_live, _SCHEDULE_CACHE_TTL)
        return payload

    # No snapshot — must page live (slow). Still cache the result.
    payload = build_live()
    _cache_set(cache_key, payload, _SCHEDULE_CACHE_TTL)
    return payload


@router.get("/schedule/today", dependencies=[Security(require_api_key)])
def schedule_today(workspace_id: Optional[str] = None, date: Optional[str] = None):
    """Sending schedule for one UTC send day, per campaign and per inbox.

    Defaults to the current UTC day; pass `date` (YYYY-MM-DD) for another day.
    Current/past days with a midnight snapshot return a fast estimate first
    (approximate=true); a background job replaces the cache with exact live
    remaining (excludes overdue). Future days with a pre-capture are instant.

    Each workspace is cached ~10 minutes independently. Omitting workspace_id
    (or passing "all") sums those same cached entries, so all == v1 + v2.
    """
    utc_today = datetime.now(timezone.utc).date().isoformat()
    today = utc_today
    if date:
        try:
            today = datetime.strptime(date[:10], "%Y-%m-%d").date().isoformat()
        except ValueError:
            pass

    ws_filter = workspace_id if workspace_id and workspace_id != "all" else None
    pollable = config.pollable_workspaces()
    workspaces = (
        [w for w in pollable if w["id"] == ws_filter]
        if ws_filter else pollable
    )
    if not workspaces:
        return {
            "date": today,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "planned_total": 0,
            "remaining_total": 0,
            "overdue_total": None,
            "plan_total": None,
            "sent_total": None,
            "approximate": False,
            "campaigns": [],
        }

    snapshot = _plan_snapshot(today, [w["id"] for w in workspaces])
    if len(workspaces) == 1:
        ws = workspaces[0]
        return _get_workspace_schedule(
            ws, today, utc_today, snapshot.get(ws["id"]) or []
        )

    with ThreadPoolExecutor(max_workers=len(workspaces)) as pool:
        parts = list(
            pool.map(
                lambda ws: _get_workspace_schedule(
                    ws, today, utc_today, snapshot.get(ws["id"]) or []
                ),
                workspaces,
            )
        )
    return _merge_workspaces(parts, today)
