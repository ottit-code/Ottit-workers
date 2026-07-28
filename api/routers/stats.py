"""Auto-extracted from the former monolithic api/main.py.

Route handlers for this domain. Shared auth/cache/helpers come from api.deps.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

import httpx  # noqa: F401  (kept uniform across routers)
from fastapi import APIRouter, Request, HTTPException, Header, Security
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from lib import emailbison, emailguard, config  # noqa: F401
from lib.supabase_client import get_supabase
from api.logging_utils import log_action
from lib.notifications import create_notification
from api.deps import (  # noqa: F401
    require_api_key,
    _today,
    _bearer,
    _cache_get,
    _cache_set,
    _compute_warm_state,
    WarmState,
    _NOTIFICATION_COLS,
    ReviewState,
    Classification,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------


@router.get("/workspaces", dependencies=[Security(require_api_key)])
def list_workspaces():
    """Configured workspaces (id + name). The frontend switcher uses this."""
    return {
        "workspaces": [
            {"id": ws["id"], "name": ws["name"]} for ws in config.WORKSPACES
        ],
        "default_workspace_id": config.DEFAULT_WORKSPACE_ID,
    }


# ---------------------------------------------------------------------------
# Stats overview
# ---------------------------------------------------------------------------

_WORKSPACE_STATS_COLS = "stat_date,emails_sent,emails_opened,emails_replied,emails_bounced,unsubscribed,interested"
_STATS_METRIC_KEYS = ("emails_sent", "emails_opened", "emails_replied", "emails_bounced", "unsubscribed", "interested")


def _sum_metrics(rows: list[dict]) -> dict:
    return {k: sum((r.get(k) or 0) for r in rows) for k in _STATS_METRIC_KEYS}


class StatTotals(BaseModel):
    emails_sent: int
    emails_opened: int
    emails_replied: int
    emails_bounced: int
    unsubscribed: int
    interested: int


class StatsResponse(BaseModel):
    period_days: int
    totals: StatTotals
    by_date: List[dict]
    prior_totals: StatTotals = Field(
        description="Aggregates covering the window of the same length immediately prior to the current period."
    )
    prior_period_days: int


@router.get("/stats", dependencies=[Security(require_api_key)], response_model=StatsResponse)
def get_stats(
    days: int = 30,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    workspace_id: Optional[str] = None,
):
    """
    Workspace-level email stats from workspace_daily_stats (written by stats_poller).
    Returns per-day rows + aggregated totals plus prior-period totals (same length
    window immediately before) for delta computation on the client.

    Either pass `days` (last N days ending today) or an explicit `start_date` /
    `end_date` range (YYYY-MM-DD, inclusive). Explicit dates take precedence.
    `workspace_id` filters to one workspace; omitted or "all" aggregates all.
    """
    supabase = get_supabase()
    today = datetime.now(timezone.utc).date()

    if start_date or end_date:
        try:
            range_end = datetime.fromisoformat(end_date).date() if end_date else today
            range_start = (
                datetime.fromisoformat(start_date).date()
                if start_date
                else range_end - timedelta(days=days)
            )
        except ValueError:
            raise HTTPException(status_code=422, detail="Dates must be YYYY-MM-DD")
        if range_start > range_end:
            raise HTTPException(status_code=422, detail="start_date must be <= end_date")
        period_days = (range_end - range_start).days + 1
        since = range_start.isoformat()
        until = (range_end + timedelta(days=1)).isoformat()
        prior_since = (range_start - timedelta(days=period_days)).isoformat()
        prior_until = since
    else:
        period_days = days
        since = (today - timedelta(days=days)).isoformat()
        until = (today + timedelta(days=1)).isoformat()
        prior_since = (today - timedelta(days=days * 2)).isoformat()
        prior_until = since

    ws_filter = workspace_id if workspace_id and workspace_id != "all" else None

    def _query(low: str, high: str) -> list[dict]:
        q = supabase.table("workspace_daily_stats").select(_WORKSPACE_STATS_COLS).gte(
            "stat_date", low
        ).lt("stat_date", high)
        if ws_filter:
            q = q.eq("workspace_id", ws_filter)
        return q.order("stat_date").execute().data or []

    def _merge_by_date(rows: list[dict]) -> list[dict]:
        """Collapse per-workspace rows into one row per date (for 'all')."""
        merged: dict[str, dict] = {}
        for r in rows:
            d = r.get("stat_date")
            if d not in merged:
                merged[d] = {"stat_date": d, **{k: 0 for k in _STATS_METRIC_KEYS}}
            for k in _STATS_METRIC_KEYS:
                merged[d][k] += r.get(k) or 0
        return [merged[d] for d in sorted(merged)]

    try:
        current = _merge_by_date(_query(since, until))
        prior = _query(prior_since, prior_until)
        return {
            "period_days": period_days,
            "totals": _sum_metrics(current),
            "by_date": current,
            "prior_totals": _sum_metrics(prior),
            "prior_period_days": period_days,
        }
    except Exception:
        raise


