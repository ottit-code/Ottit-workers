"""Shared FastAPI dependencies, auth, TTL cache, and cross-router helpers.

Extracted from the former monolithic api/main.py so the per-domain router
modules can share auth, the counts cache, and a few small helpers/constants
without importing each other.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Literal

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from lib import config

_bearer = HTTPBearer(auto_error=False)


def require_api_key(credentials: HTTPAuthorizationCredentials = Security(_bearer)):
    """Validate Bearer token.

    Fail-closed: if API_KEY is unset the endpoint is only left open in
    development (APP_ENV=development). In any other environment a missing key
    returns 503 rather than silently disabling auth.
    """
    if not config.API_KEY:
        if config.APP_ENV == "development":
            return
        raise HTTPException(status_code=503, detail="API_KEY not configured")
    if credentials is None or credentials.credentials != config.API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# ---------------------------------------------------------------------------
# In-process TTL cache (used by /counts). Per-process, not shared across
# workers — acceptable at current scale.
# ---------------------------------------------------------------------------
_CACHE: Dict[str, tuple] = {}
_CACHE_LOCK = threading.Lock()


def _cache_get(key: str) -> Any:
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
    if entry is None:
        return None
    expires, value = entry
    if expires < time.time():
        return None
    return value


def _cache_set(key: str, value: Any, ttl: int) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.time() + ttl, value)


def _cache_get_stale(key: str) -> tuple:
    """(value, fresh) for stale-while-revalidate callers.

    Unlike _cache_get, expired entries are still returned (with fresh=False)
    so slow-to-build endpoints can serve the old payload instantly while a
    background thread rebuilds. _cache_clear() removes entries outright, so a
    manual data refresh still forces a blocking rebuild.
    """
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
    if entry is None:
        return None, False
    expires, value = entry
    return value, expires >= time.time()


_REVALIDATING: set = set()


def _cache_revalidate(key: str, build, ttl: int) -> None:
    """Rebuild `key` via build() on a daemon thread, once at a time per key."""
    with _CACHE_LOCK:
        if key in _REVALIDATING:
            return
        _REVALIDATING.add(key)

    def _work():
        try:
            _cache_set(key, build(), ttl)
        except Exception:  # keep serving stale on failure
            logging.getLogger(__name__).warning(
                f"background revalidation failed for {key}", exc_info=True
            )
        finally:
            with _CACHE_LOCK:
                _REVALIDATING.discard(key)

    threading.Thread(target=_work, daemon=True).start()


def _cache_clear() -> None:
    """Drop every cached entry (used by the manual data refresh)."""
    with _CACHE_LOCK:
        _CACHE.clear()


# ---------------------------------------------------------------------------
# Cohort reply rate (sent-date attribution)
# ---------------------------------------------------------------------------

def _cohort_reply_map(
    start: str, end: str, group: str, workspace_id: str | None = None
) -> Dict[tuple, int]:
    """(group_key, stat_date) → replies attributed to the original sent date.

    Backed by the get_cohort_reply_counts RPC (migration 012); group is
    'campaign' | 'sender' | 'tld' | 'tag'. Automated replies are excluded.
    """
    from lib.supabase_client import get_supabase
    from lib.supabase_paginate import fetch_all

    params: Dict[str, Any] = {"p_start": start, "p_end": end, "p_group": group}
    if workspace_id:
        params["p_workspace_id"] = workspace_id
    # Paged: group × day rows over long ranges exceed the 1000-row cap.
    rows = fetch_all(lambda: get_supabase().rpc("get_cohort_reply_counts", params))
    return {
        (str(r["group_key"]), str(r["stat_date"])): int(r["cohort_replies"] or 0)
        for r in rows
    }


def _merge_cohort_fields(
    rows: list, key_field: str, cohort: Dict[tuple, int]
) -> list:
    """Attach cohort_replies + cohort_reply_rate (%) to per-day rows in place."""
    for r in rows:
        replies = cohort.get((str(r.get(key_field)), str(r.get("stat_date"))), 0)
        sent = r.get("emails_sent") or 0
        r["cohort_replies"] = replies
        r["cohort_reply_rate"] = round(replies / sent * 100, 4) if sent else 0.0
    return rows


# ---------------------------------------------------------------------------
# Cross-router constants / helpers
# ---------------------------------------------------------------------------
_NOTIFICATION_COLS = (
    "id,severity,type,entity_type,entity_id,title,body,read,resolved,created_at"
)

ReviewState = Literal["pending", "classified", "snoozed", "archived"]
Classification = Literal["interested", "not_interested", "question", "auto_reply", "ooo"]

WarmState = Literal["cold", "warming", "warmed", "throttled"]


def _compute_warm_state(row: dict) -> str:
    if row.get("in_recovery"):
        return "throttled"
    score = row.get("warmup_score")
    if score is None:
        return "cold"
    if score >= 70:
        return "warmed"
    if score >= 40:
        return "warming"
    return "cold"
