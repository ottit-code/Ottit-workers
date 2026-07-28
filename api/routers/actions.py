"""Auto-extracted from the former monolithic api/main.py.

Route handlers for this domain. Shared auth/cache/helpers come from api.deps.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
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
    _cache_clear,
    _compute_warm_state,
    WarmState,
    _NOTIFICATION_COLS,
    ReviewState,
    Classification,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# Campaign actions
# ---------------------------------------------------------------------------

@router.post("/actions/campaigns/{campaign_id}/pause", dependencies=[Security(require_api_key)])
def pause_campaign(campaign_id: str, x_user_email: str = Header(default=None)):
    try:
        result = emailbison.patch(f"/api/campaigns/{campaign_id}/pause")
        log_action("campaign_pause", "campaign", campaign_id, payload={"campaign_id": campaign_id}, api_response=result, performed_by=x_user_email)
        return {"success": True, "data": result}
    except Exception as e:
        log_action("campaign_pause", "campaign", campaign_id, status="error", error_message=str(e), performed_by=x_user_email)
        raise


@router.post("/actions/campaigns/{campaign_id}/resume", dependencies=[Security(require_api_key)])
def resume_campaign(campaign_id: str, x_user_email: str = Header(default=None)):
    try:
        result = emailbison.patch(f"/api/campaigns/{campaign_id}/resume")
        log_action("campaign_resume", "campaign", campaign_id, payload={"campaign_id": campaign_id}, api_response=result, performed_by=x_user_email)
        return {"success": True, "data": result}
    except Exception as e:
        log_action("campaign_resume", "campaign", campaign_id, status="error", error_message=str(e), performed_by=x_user_email)
        raise


@router.post("/actions/campaigns/{campaign_id}/archive", dependencies=[Security(require_api_key)])
def archive_campaign(campaign_id: str, x_user_email: str = Header(default=None)):
    try:
        result = emailbison.patch(f"/api/campaigns/{campaign_id}/archive")
        log_action("campaign_archive", "campaign", campaign_id, payload={"campaign_id": campaign_id}, api_response=result, performed_by=x_user_email)
        return {"success": True, "data": result}
    except Exception as e:
        log_action("campaign_archive", "campaign", campaign_id, status="error", error_message=str(e), performed_by=x_user_email)
        raise


@router.post("/actions/campaigns/{campaign_id}/duplicate", dependencies=[Security(require_api_key)])
def duplicate_campaign(campaign_id: str, x_user_email: str = Header(default=None)):
    try:
        result = emailbison.post(f"/api/campaigns/{campaign_id}/duplicate")
        log_action("campaign_duplicate", "campaign", campaign_id, payload={"campaign_id": campaign_id}, api_response=result, performed_by=x_user_email)
        return {"success": True, "data": result}
    except Exception as e:
        log_action("campaign_duplicate", "campaign", campaign_id, status="error", error_message=str(e), performed_by=x_user_email)
        raise


# ---------------------------------------------------------------------------
# Sender actions
# ---------------------------------------------------------------------------

class UpdateDailyLimitRequest(BaseModel):
    daily_limit: int


@router.patch("/actions/senders/{sender_id}/daily-limit", dependencies=[Security(require_api_key)])
def update_daily_limit(sender_id: str, body: UpdateDailyLimitRequest, x_user_email: str = Header(default=None)):
    try:
        result = emailbison.patch(f"/api/sender-emails/{sender_id}", {"daily_limit": body.daily_limit})
        log_action("sender_update_daily_limit", "sender", sender_id, payload=body.dict(), api_response=result, performed_by=x_user_email)
        return {"success": True, "data": result}
    except Exception as e:
        log_action("sender_update_daily_limit", "sender", sender_id, status="error", error_message=str(e), performed_by=x_user_email)
        raise


class ToggleWarmupRequest(BaseModel):
    enable: bool


@router.patch("/actions/senders/{sender_id}/warmup", dependencies=[Security(require_api_key)])
def toggle_warmup(sender_id: str, body: ToggleWarmupRequest, x_user_email: str = Header(default=None)):
    path = "/api/warmup/sender-emails/enable" if body.enable else "/api/warmup/sender-emails/disable"
    try:
        result = emailbison.patch(path, {"sender_email_ids": [sender_id]})
        log_action("sender_toggle_warmup", "sender", sender_id, payload=body.dict(), api_response=result, performed_by=x_user_email)
        return {"success": True, "data": result}
    except Exception as e:
        log_action("sender_toggle_warmup", "sender", sender_id, status="error", error_message=str(e), performed_by=x_user_email)
        raise


# ---------------------------------------------------------------------------
# Reply actions
# ---------------------------------------------------------------------------

def _upsert_review_state(reply_id: str, fields: dict) -> None:
    row = {"reply_id": reply_id, "updated_at": datetime.now(timezone.utc).isoformat(), **fields}
    try:
        get_supabase().table("reply_review_state").upsert(row, on_conflict="reply_id").execute()
    except Exception as e:
        logger.error(f"Failed to upsert reply_review_state for {reply_id}: {e}")


@router.post("/actions/replies/{reply_id}/mark-interested", dependencies=[Security(require_api_key)])
def mark_interested(reply_id: str, x_user_email: str = Header(default=None)):
    try:
        result = emailbison.patch(f"/api/replies/{reply_id}/interested")
        _upsert_review_state(reply_id, {"review_state": "classified", "classification": "interested"})
        log_action("reply_mark_interested", "reply", reply_id, api_response=result, performed_by=x_user_email)
        return {"success": True}
    except Exception as e:
        log_action("reply_mark_interested", "reply", reply_id, status="error", error_message=str(e), performed_by=x_user_email)
        raise


@router.post("/actions/replies/{reply_id}/mark-not-interested", dependencies=[Security(require_api_key)])
def mark_not_interested(reply_id: str, x_user_email: str = Header(default=None)):
    try:
        result = emailbison.patch(f"/api/replies/{reply_id}/uninterested")
        _upsert_review_state(reply_id, {"review_state": "classified", "classification": "not_interested"})
        log_action("reply_mark_not_interested", "reply", reply_id, api_response=result, performed_by=x_user_email)
        return {"success": True}
    except Exception as e:
        log_action("reply_mark_not_interested", "reply", reply_id, status="error", error_message=str(e), performed_by=x_user_email)
        raise


class ReviewStateRequest(BaseModel):
    state: ReviewState


@router.patch("/actions/replies/{reply_id}/read", dependencies=[Security(require_api_key)])
def mark_reply_read(reply_id: str, x_user_email: str = Header(default=None)):
    supabase = get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    try:
        existing = (
            supabase.table("reply_review_state")
            .select("reply_id,first_read_at")
            .eq("reply_id", reply_id)
            .limit(1)
            .execute()
            .data or []
        )
        row: dict = {"reply_id": reply_id, "read": True, "updated_at": now}
        if not existing or not existing[0].get("first_read_at"):
            row["first_read_at"] = now
        supabase.table("reply_review_state").upsert(row, on_conflict="reply_id").execute()
        log_action("reply_mark_read", "reply", reply_id, performed_by=x_user_email)
        return {"success": True}
    except Exception as e:
        log_action("reply_mark_read", "reply", reply_id, status="error", error_message=str(e), performed_by=x_user_email)
        raise


@router.patch("/actions/replies/{reply_id}/review-state", dependencies=[Security(require_api_key)])
def update_review_state(reply_id: str, body: ReviewStateRequest, x_user_email: str = Header(default=None)):
    try:
        _upsert_review_state(reply_id, {"review_state": body.state})
        log_action("reply_update_review_state", "reply", reply_id, payload=body.dict(), performed_by=x_user_email)
        return {"success": True}
    except Exception as e:
        log_action("reply_update_review_state", "reply", reply_id, status="error", error_message=str(e), performed_by=x_user_email)
        raise


class SendReplyRequest(BaseModel):
    to: str
    subject: str
    body: str
    campaign_id: Optional[str] = None


@router.post("/actions/replies/send", dependencies=[Security(require_api_key)])
def send_reply(body: SendReplyRequest, x_user_email: str = Header(default=None)):
    try:
        result = emailbison.post("/api/replies/new", body.dict())
        log_action("reply_send", "reply", None, payload={"to": body.to, "subject": body.subject}, api_response=result, performed_by=x_user_email)
        return {"success": True, "data": result}
    except Exception as e:
        log_action("reply_send", "reply", None, status="error", error_message=str(e), performed_by=x_user_email)
        raise


# ---------------------------------------------------------------------------
# Deliverability actions
# ---------------------------------------------------------------------------

class TriggerTestRequest(BaseModel):
    domain: Optional[str] = None
    sender_email: Optional[str] = None


@router.post("/actions/deliverability/placement-test", dependencies=[Security(require_api_key)])
def trigger_placement_test(body: TriggerTestRequest, x_user_email: str = Header(default=None)):
    try:
        result = emailguard.post("/api/v1/inbox-placement-tests", body.dict(exclude_none=True))
        log_action("trigger_placement_test", "domain", body.domain, payload=body.dict(), api_response=result, performed_by=x_user_email)
        return {"success": True, "data": result}
    except Exception as e:
        log_action("trigger_placement_test", "domain", body.domain, status="error", error_message=str(e), performed_by=x_user_email)
        raise


@router.post("/actions/deliverability/spam-test", dependencies=[Security(require_api_key)])
def trigger_spam_test(body: TriggerTestRequest, x_user_email: str = Header(default=None)):
    try:
        result = emailguard.post("/api/v1/spam-filter-tests", body.dict(exclude_none=True))
        log_action("trigger_spam_test", "domain", body.domain, payload=body.dict(), api_response=result, performed_by=x_user_email)
        return {"success": True, "data": result}
    except Exception as e:
        log_action("trigger_spam_test", "domain", body.domain, status="error", error_message=str(e), performed_by=x_user_email)
        raise


@router.post("/actions/deliverability/surbl-check", dependencies=[Security(require_api_key)])
def trigger_surbl_check(body: TriggerTestRequest, x_user_email: str = Header(default=None)):
    try:
        result = emailguard.post("/api/v1/surbl-blacklist-checks/domains", body.dict(exclude_none=True))
        log_action("trigger_surbl_check", "domain", body.domain, payload=body.dict(), api_response=result, performed_by=x_user_email)
        return {"success": True, "data": result}
    except Exception as e:
        log_action("trigger_surbl_check", "domain", body.domain, status="error", error_message=str(e), performed_by=x_user_email)
        raise


# ---------------------------------------------------------------------------
# Manual data refresh
# ---------------------------------------------------------------------------
# Re-runs the READ-ONLY pollers on demand so the dashboard reflects the latest
# state from EmailBison / EmailGuard. Never triggers anything on the source
# side (no new placement tests, no warmup actions) — placement_schedule_runner
# and the trigger_* actions above are deliberately excluded.

_REFRESH_TIMEOUT_SECONDS = 180
_refresh_lock = threading.Lock()
_last_refresh: Dict[str, Any] = {"refreshed_at": None, "sources": []}


def _refresh_sources() -> list[tuple[str, Any]]:
    # Imported lazily so the API process only pays for worker imports on use.
    from workers import (
        stats_poller,
        campaign_daily_stats_poller,
        sender_performance_poller,
        reply_events_poller,
        delivery_poller,
        inboxassure_poller,
    )
    from lib import inboxassure

    sources: list[tuple[str, Any]] = [
        ("sender_and_workspace_stats", stats_poller.run),
        ("campaign_daily_stats", campaign_daily_stats_poller.run),
        ("sender_performance", sender_performance_poller.run),
        ("reply_events", reply_events_poller.run),
        ("deliverability_results", delivery_poller.run),
    ]
    if inboxassure.is_configured():
        sources.append(("inboxassure_placement", inboxassure_poller.run))
    return sources


@router.get("/actions/refresh", dependencies=[Security(require_api_key)])
def get_refresh_status():
    """Last manual refresh result + whether one is currently running."""
    return {**_last_refresh, "in_progress": _refresh_lock.locked()}


@router.post("/actions/refresh", dependencies=[Security(require_api_key)])
async def refresh_data(x_user_email: str = Header(default=None)):
    """Pull the latest state from every source (read-only) and update all views."""
    if not _refresh_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A refresh is already in progress")

    def _run_all() -> Dict[str, Any]:
        results: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=len(_refresh_sources())) as pool:
            futures = {
                name: (pool.submit(fn), time.time())
                for name, fn in _refresh_sources()
            }
            for name, (future, started) in futures.items():
                entry: Dict[str, Any] = {"source": name, "ok": True, "error": None}
                try:
                    future.result(timeout=_REFRESH_TIMEOUT_SECONDS)
                except FuturesTimeoutError:
                    entry.update(ok=False, error=f"timed out after {_REFRESH_TIMEOUT_SECONDS}s")
                except Exception as e:  # poller run() already logs internally
                    entry.update(ok=False, error=str(e))
                entry["seconds"] = round(time.time() - started, 1)
                results.append(entry)
        return {
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
            "sources": results,
        }

    try:
        outcome = await run_in_threadpool(_run_all)
        _last_refresh.update(outcome)
        _cache_clear()
        log_action(
            "data_refresh", "system", "all",
            payload={"sources": [s["source"] for s in outcome["sources"]]},
            api_response={"failures": [s for s in outcome["sources"] if not s["ok"]]},
            performed_by=x_user_email,
        )
        return {**outcome, "in_progress": False}
    finally:
        _refresh_lock.release()
