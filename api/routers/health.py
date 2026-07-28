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
# Health
# ---------------------------------------------------------------------------

def _probe_supabase() -> str:
    try:
        get_supabase().table("notifications").select("id").limit(1).execute()
        return "ok"
    except Exception as e:
        return f"error: {e}"


def _probe_emailbison() -> str:
    try:
        emailbison.get("/api/sender-emails", params={"per_page": 1})
        return "ok"
    except Exception as e:
        return f"error: {e}"


def _probe_emailguard() -> str:
    try:
        emailguard.get("/api/v1/inbox-placement-tests", params={"per_page": 1})
        return "ok"
    except Exception as e:
        return f"error: {e}"


@router.get("/health")
def health():
    with ThreadPoolExecutor(max_workers=3) as pool:
        sb_f = pool.submit(_probe_supabase)
        eb_f = pool.submit(_probe_emailbison)
        eg_f = pool.submit(_probe_emailguard)
        checks = {
            "supabase": sb_f.result(),
            "emailbison": eb_f.result(),
            "emailguard": eg_f.result(),
        }
    overall = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    return {"status": overall, **checks}


