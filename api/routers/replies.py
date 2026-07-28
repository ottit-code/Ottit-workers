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
# Leads & Replies — live from EmailBison (no Supabase table yet)
# ---------------------------------------------------------------------------

@router.get("/leads", dependencies=[Security(require_api_key)])
def list_leads(campaign_id: Optional[str] = None):
    """Live from EmailBison. Optionally filter by campaign_id."""
    try:
        leads = emailbison.get_leads(campaign_id=campaign_id)
        return [
            {
                "id": l.get("id"),
                "email": l.get("email") or l.get("lead_email") or "",
                "first_name": l.get("first_name") or l.get("firstName"),
                "last_name": l.get("last_name") or l.get("lastName"),
                "company": l.get("company") or l.get("organization"),
                "status": l.get("status"),
                "campaign_id": l.get("campaign_id"),
                "created_at": l.get("created_at") or l.get("createdAt"),
            }
            for l in leads
        ]
    except Exception:
        raise


ReviewState = Literal["pending", "classified", "snoozed", "archived"]
Classification = Literal["interested", "not_interested", "question", "auto_reply", "ooo"]


class Reply(BaseModel):
    id: str
    lead_email: Optional[str] = None
    from_name: Optional[str] = None
    subject: Optional[str] = None
    campaign_id: Optional[str] = None
    body: Optional[str] = None
    created_at: Optional[str] = None
    interested: Optional[bool] = None
    review_state: ReviewState = Field("pending", description="Human review state. 'pending' until classified.")
    read: bool = False
    first_read_at: Optional[str] = None
    classification: Optional[Classification] = None


def _fetch_review_states(reply_ids: list[str]) -> dict[str, dict]:
    if not reply_ids:
        return {}
    try:
        rows = (
            get_supabase()
            .table("reply_review_state")
            .select("reply_id,review_state,read,first_read_at,classification")
            .in_("reply_id", reply_ids)
            .execute()
            .data or []
        )
        return {r["reply_id"]: r for r in rows}
    except Exception as e:
        logger.warning(f"reply_review_state lookup failed: {e}")
        return {}


def _normalize_reply(r: dict, state: Optional[dict]) -> dict:
    reply_id = str(r.get("id"))
    base = {
        "id": reply_id,
        "lead_email": r.get("from_email_address") or r.get("lead_email") or r.get("from_email") or r.get("email"),
        "from_name": r.get("from_name"),
        "subject": r.get("subject"),
        "campaign_id": str(r.get("campaign_id")) if r.get("campaign_id") is not None else None,
        "body": r.get("text_body") or r.get("body") or r.get("message") or r.get("content"),
        "created_at": r.get("date_received") or r.get("created_at"),
        "interested": r.get("interested") if r.get("interested") is not None else r.get("is_interested"),
    }
    if state:
        base.update({
            "review_state": state.get("review_state") or "pending",
            "read": bool(state.get("read")),
            "first_read_at": state.get("first_read_at"),
            "classification": state.get("classification"),
        })
    else:
        base.update({"review_state": "pending", "read": False, "first_read_at": None, "classification": None})
    return base


@router.get("/replies", dependencies=[Security(require_api_key)], response_model=List[Reply])
def list_replies(campaign_id: Optional[str] = None):
    """Live from EmailBison merged with persisted review state from reply_review_state."""
    try:
        replies = emailbison.get_replies(campaign_id=campaign_id)
        reply_ids = [str(r.get("id")) for r in replies if r.get("id") is not None]
        states = _fetch_review_states(reply_ids)
        return [_normalize_reply(r, states.get(str(r.get("id")))) for r in replies]
    except Exception:
        raise


# ---------------------------------------------------------------------------
# Reply events — from Supabase (written by reply_events_poller)
# ---------------------------------------------------------------------------

_REPLY_EVENT_COLS = (
    "reply_id,campaign_id,campaign_name,lead_id,lead_email,"
    "sender_email_id,sender_email,sequence_step_id,classification,"
    "folder,replied_at,original_sent_at,response_time_hours,"
    "subject,has_attachment,is_thread_reply,fetched_at"
)


@router.get("/reply-events", dependencies=[Security(require_api_key)])
def list_reply_events(
    campaign_id: Optional[str] = None,
    classification: Optional[str] = None,
    lead_email: Optional[str] = None,
    days: int = 30,
    limit: int = 200,
):
    """
    Reply events from reply_events (polled every 4 hours).
    Filter by campaign_id, classification (interested/not_automated_reply/automated_reply),
    or lead_email. Returns most recent N days.
    """
    supabase = get_supabase()
    since = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    try:
        query = (
            supabase.table("reply_events")
            .select(_REPLY_EVENT_COLS)
            .gte("replied_at", since)
            .order("replied_at", desc=True)
            .limit(limit)
        )
        if campaign_id:
            query = query.eq("campaign_id", campaign_id)
        if classification:
            query = query.eq("classification", classification)
        if lead_email:
            query = query.eq("lead_email", lead_email)
        return query.execute().data
    except Exception:
        raise


# ---------------------------------------------------------------------------
# Lead engagement snapshots — from Supabase (written by lead_engagement_poller)
# ---------------------------------------------------------------------------

_LEAD_ENGAGEMENT_COLS = (
    "lead_id,snapshot_date,first_name,last_name,email,title,company,"
    "status,tags,emails_sent,opens,unique_opens,replies,unique_replies,"
    "engagement_score,funnel_stage,campaign_engagements,custom_variables,fetched_at"
)


@router.get("/lead-engagement", dependencies=[Security(require_api_key)])
def list_lead_engagement(
    funnel_stage: Optional[str] = None,
    snapshot_date: Optional[str] = None,
    limit: int = 100,
):
    """
    Lead engagement snapshots from lead_engagement_snapshots (polled daily at 2 AM).
    Defaults to today's snapshot. Filter by funnel_stage:
    uploaded / contacted / opened / replied / interested.
    """
    supabase = get_supabase()
    date = snapshot_date or _today()
    try:
        query = (
            supabase.table("lead_engagement_snapshots")
            .select(_LEAD_ENGAGEMENT_COLS)
            .eq("snapshot_date", date)
            .order("engagement_score", desc=True)
            .limit(limit)
        )
        if funnel_stage:
            query = query.eq("funnel_stage", funnel_stage)
        return query.execute().data
    except Exception:
        raise


@router.get("/lead-engagement/{lead_id}", dependencies=[Security(require_api_key)])
def get_lead_engagement(lead_id: str, days: int = 30):
    """Historical engagement snapshots for a specific lead."""
    supabase = get_supabase()
    since = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    try:
        result = (
            supabase.table("lead_engagement_snapshots")
            .select(_LEAD_ENGAGEMENT_COLS)
            .eq("lead_id", lead_id)
            .gte("snapshot_date", since)
            .order("snapshot_date", desc=True)
            .execute()
        )
        if not result.data:
            raise HTTPException(status_code=404, detail="No engagement data for this lead")
        return result.data
    except HTTPException:
        raise
    except Exception:
        raise


