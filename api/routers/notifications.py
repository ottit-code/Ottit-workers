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
# Notifications — from Supabase
# ---------------------------------------------------------------------------

_NOTIFICATION_COLS = "id,severity,type,entity_type,entity_id,title,body,read,resolved,created_at"


@router.get("/notifications", dependencies=[Security(require_api_key)])
def list_notifications(
    unread: Optional[bool] = None,
    resolved: Optional[bool] = None,
    severity: Optional[str] = None,
    entity_type: Optional[str] = None,
    limit: int = 100,
):
    supabase = get_supabase()
    try:
        query = supabase.table("notifications").select(_NOTIFICATION_COLS).order("created_at", desc=True).limit(limit)
        if unread is not None:
            query = query.eq("read", not unread)
        if resolved is not None:
            query = query.eq("resolved", resolved)
        if severity:
            query = query.eq("severity", severity)
        if entity_type:
            query = query.eq("entity_type", entity_type)
        return query.execute().data
    except Exception:
        raise


@router.patch("/notifications/{notification_id}/read", dependencies=[Security(require_api_key)])
def mark_notification_read(notification_id: int):
    supabase = get_supabase()
    try:
        result = supabase.table("notifications").update({"read": True}).eq("id", notification_id).execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Notification not found")
        return {"success": True}
    except HTTPException:
        raise
    except Exception:
        raise


@router.patch("/notifications/{notification_id}/resolve", dependencies=[Security(require_api_key)])
def resolve_notification(notification_id: int):
    supabase = get_supabase()
    try:
        result = supabase.table("notifications").update({"resolved": True, "read": True}).eq("id", notification_id).execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Notification not found")
        return {"success": True}
    except HTTPException:
        raise
    except Exception:
        raise


@router.post("/notifications/read-all", dependencies=[Security(require_api_key)])
def mark_all_notifications_read():
    supabase = get_supabase()
    try:
        supabase.table("notifications").update({"read": True}).eq("read", False).execute()
        return {"success": True}
    except Exception:
        raise


