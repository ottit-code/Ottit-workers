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
from lib.supabase_paginate import fetch_all
from api.logging_utils import log_action
from lib.notifications import create_notification
from api.deps import (  # noqa: F401
    require_api_key,
    _today,
    _bearer,
    _cache_get,
    _cache_get_stale,
    _cache_revalidate,
    _cache_set,
    _cohort_reply_map,
    _merge_cohort_fields,
    _compute_warm_state,
    WarmState,
    _NOTIFICATION_COLS,
    ReviewState,
    Classification,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# Campaigns — live from EmailBison
# ---------------------------------------------------------------------------

class Campaign(BaseModel):
    campaign_id: str
    campaign_name: str
    campaign_status: str
    emails_sent_count: int
    reply_count: int
    bounced_count: int
    total_leads: int
    completion_percentage: float
    created_at: Optional[str] = None
    opened_count: int = Field(0, description="Total opens (not unique) from EmailBison.")
    unique_opens_count: int = Field(0, description="Unique opens from EmailBison.")
    interested_count: int = Field(0, description="Leads marked interested.")
    booked_count: int = Field(0, description="Leads with a confirmed meeting. Equals interested_count until calendar integration ships.")
    last_sent_at: Optional[str] = Field(None, description="Timestamp of most recent send activity on the campaign (EmailBison updated_at).")


def _normalize_campaign(c: dict) -> dict:
    interested = int(c.get("interested") or 0)
    return {
        "campaign_id": str(c.get("id")),
        "campaign_name": c.get("name", ""),
        "campaign_status": str(c.get("status") or "unknown").lower(),
        "emails_sent_count": int(c.get("emails_sent") or 0),
        "reply_count": int(c.get("replied") or 0),
        "bounced_count": int(c.get("bounced") or 0),
        "total_leads": int(c.get("total_leads") or 0),
        "completion_percentage": float(c.get("completion_percentage") or 0),
        "created_at": c.get("created_at"),
        "opened_count": int(c.get("opened") or 0),
        "unique_opens_count": int(c.get("unique_opens") or 0),
        "interested_count": interested,
        "booked_count": interested,
        "last_sent_at": c.get("updated_at"),
    }


@router.get("/campaigns", dependencies=[Security(require_api_key)], response_model=List[Campaign])
def list_campaigns(status: Optional[str] = None, workspace_id: Optional[str] = None):
    """
    Live campaigns from EmailBison with real stats.
    Optionally filter by status: active, paused, archived, completed, draft.
    `workspace_id` selects one workspace; omitted or "all" merges every workspace.
    """
    try:
        # Live Bison fetch now walks every page (~11+ requests per workspace),
        # so cache briefly per workspace, serving stale entries while a
        # background thread revalidates. The manual data refresh clears this.
        def _fetch_ws_campaigns(ws_id: str) -> list:
            cache_key = f"bison_campaigns:{ws_id}"
            build = lambda: emailbison.for_workspace(ws_id).get_campaigns()  # noqa: E731
            cached, fresh = _cache_get_stale(cache_key)
            if fresh:
                return cached
            if cached is not None:
                _cache_revalidate(cache_key, build, ttl=180)
                return cached
            result = build()
            _cache_set(cache_key, result, ttl=180)
            return result

        if workspace_id and workspace_id != "all":
            ws = config.get_workspace(workspace_id)
            if ws is not None and not ws.get("bison_token"):
                # Registered but not yet connected (e.g. ws_v2 before its
                # token is configured) — no live data, not an error.
                return []
            campaigns = _fetch_ws_campaigns(workspace_id)
        else:
            campaigns = []
            for ws in config.pollable_workspaces():
                try:
                    campaigns.extend(_fetch_ws_campaigns(ws["id"]))
                except Exception as e:
                    logger.warning(f"campaigns fetch failed for workspace {ws['id']}: {e}")
        normalized = [_normalize_campaign(c) for c in campaigns]
        if status:
            wanted = status.lower()
            normalized = [c for c in normalized if c["campaign_status"] == wanted]
        return normalized
    except Exception:
        raise


# ---------------------------------------------------------------------------
# Campaign daily stats — from Supabase (written by campaign_daily_stats_poller)
# ---------------------------------------------------------------------------

_CAMPAIGN_STATS_COLS = (
    "campaign_id,campaign_name,campaign_status,stat_date,"
    "emails_sent,emails_opened,unique_opens,emails_replied,emails_bounced,"
    "unsubscribed,interested,open_rate,reply_rate,bounce_rate,"
    "max_emails_per_day,max_new_leads_per_day,total_leads_contacted,"
    "completion_pct,fetched_at"
)


@router.get("/campaign-stats", dependencies=[Security(require_api_key)])
def list_campaign_stats(
    campaign_id: Optional[str] = None,
    days: int = 30,
    status: Optional[str] = None,
    workspace_id: Optional[str] = None,
):
    """
    Per-campaign daily stats from campaign_daily_stats (polled daily at midnight).
    Returns rows for the last N days. Filter by campaign_id, status or workspace.
    """
    supabase = get_supabase()
    today = datetime.now(timezone.utc).date().isoformat()
    since = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    ws = workspace_id if workspace_id and workspace_id != "all" else None
    try:
        def build():
            query = (
                supabase.table("campaign_daily_stats")
                .select(_CAMPAIGN_STATS_COLS)
                .gte("stat_date", since)
                .order("stat_date", desc=True)
                .order("campaign_id")
            )
            if campaign_id:
                query = query.eq("campaign_id", campaign_id)
            if status:
                query = query.eq("campaign_status", status)
            if ws:
                query = query.eq("workspace_id", ws)
            return query

        # Paged: 150+ campaigns × N days exceeds the 1000-row cap.
        rows = fetch_all(build)
    except Exception:
        raise
    # Cohort reply rate: replies attributed to the original email's sent date.
    try:
        cohort = _cohort_reply_map(since, today, "campaign", ws)
        _merge_cohort_fields(rows, "campaign_id", cohort)
    except Exception as e:
        logger.warning(f"cohort reply merge failed for campaign-stats: {e}")
    return rows


@router.get("/campaign-stats/{campaign_id}/history", dependencies=[Security(require_api_key)])
def campaign_stats_history(campaign_id: str, days: int = 90):
    """Full date-series for a single campaign."""
    supabase = get_supabase()
    since = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    try:
        result = (
            supabase.table("campaign_daily_stats")
            .select(_CAMPAIGN_STATS_COLS)
            .eq("campaign_id", campaign_id)
            .gte("stat_date", since)
            .order("stat_date")
            .execute()
        )
        if not result.data:
            raise HTTPException(status_code=404, detail="No stats found for this campaign")
        return result.data
    except HTTPException:
        raise
    except Exception:
        raise


# ---------------------------------------------------------------------------
# A/B test snapshots — from Supabase (written by ab_test_snapshots_poller)
# ---------------------------------------------------------------------------

_AB_TEST_COLS = (
    "campaign_id,sequence_step_id,snapshot_date,email_subject,step_order,"
    "is_variant,variant_from_step_id,thread_reply,"
    "emails_sent,opens,unique_opens,clicks,replies,unique_replies,interested,bounced,"
    "open_rate,reply_rate,click_rate,interest_rate,bounce_rate,"
    "stat_confidence,stat_winner,stat_sample_sufficient,fetched_at"
)


@router.get("/ab-tests", dependencies=[Security(require_api_key)])
def list_ab_tests(
    campaign_id: Optional[str] = None,
    snapshot_date: Optional[str] = None,
    variants_only: Optional[bool] = None,
):
    """
    A/B test snapshots from ab_test_snapshots (polled every 6 hours).
    Defaults to today's snapshot. Use snapshot_date=YYYY-MM-DD for a specific date.
    Set variants_only=true to return only variant steps.
    """
    supabase = get_supabase()
    date = snapshot_date or _today()
    try:
        query = (
            supabase.table("ab_test_snapshots")
            .select(_AB_TEST_COLS)
            .eq("snapshot_date", date)
            .order("campaign_id")
            .order("step_order")
        )
        if campaign_id:
            query = query.eq("campaign_id", campaign_id)
        if variants_only is not None:
            query = query.eq("is_variant", variants_only)
        return query.execute().data
    except Exception:
        raise


@router.get("/ab-tests/{campaign_id}", dependencies=[Security(require_api_key)])
def get_ab_test_for_campaign(campaign_id: str, snapshot_date: Optional[str] = None):
    """All sequence steps + A/B stats for a specific campaign (today by default)."""
    supabase = get_supabase()
    date = snapshot_date or _today()
    try:
        result = (
            supabase.table("ab_test_snapshots")
            .select(_AB_TEST_COLS)
            .eq("campaign_id", campaign_id)
            .eq("snapshot_date", date)
            .order("step_order")
            .execute()
        )
        if not result.data:
            raise HTTPException(status_code=404, detail="No snapshot found for this campaign")
        return result.data
    except HTTPException:
        raise
    except Exception:
        raise


