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
# Deliverability — from Supabase
# ---------------------------------------------------------------------------

_DNS_HEALTH_COLS = "domain,spf_passed,spf_record,dkim_passed,dkim_selector,dmarc_passed,dmarc_policy,checked_at"


def _latest_dns_by_domain(domains: list[str]) -> dict[str, dict]:
    if not domains:
        return {}
    try:
        rows = (
            get_supabase()
            .table("dns_health_checks")
            .select(_DNS_HEALTH_COLS)
            .in_("domain", domains)
            .order("checked_at", desc=True)
            .execute()
            .data or []
        )
    except Exception as e:
        logger.warning(f"dns_health_checks lookup failed: {e}")
        return {}
    latest: dict[str, dict] = {}
    for row in rows:
        d = row.get("domain")
        if d and d not in latest:
            latest[d] = row
    return latest


@router.get("/deliverability/domain-health", dependencies=[Security(require_api_key)])
def list_domain_health(domain: Optional[str] = None):
    """
    Aggregated domain health from v_domain_health view, enriched with the latest
    SPF / DKIM / DMARC pass booleans from dns_health_checks (written by dns_check_poller).
    Includes avg spam score, placement pass/fail counts, last tested date.
    """
    supabase = get_supabase()
    try:
        query = supabase.table("v_domain_health").select("*").order("last_tested_at", desc=True)
        if domain:
            query = query.eq("domain", domain)
        rows = query.execute().data or []
        dns_map = _latest_dns_by_domain([r.get("domain") for r in rows if r.get("domain")])
        for row in rows:
            dns = dns_map.get(row.get("domain"), {})
            row["spf_passed"] = dns.get("spf_passed")
            row["spf_record"] = dns.get("spf_record")
            row["dkim_passed"] = dns.get("dkim_passed")
            row["dkim_selector"] = dns.get("dkim_selector")
            row["dmarc_passed"] = dns.get("dmarc_passed")
            row["dmarc_policy"] = dns.get("dmarc_policy")
            row["records_checked_at"] = dns.get("checked_at")
        return rows
    except Exception:
        raise

@router.get("/deliverability/placement-tests", dependencies=[Security(require_api_key)])
def list_placement_tests(
    domain: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
):
    supabase = get_supabase()
    try:
        query = supabase.table("domain_placement_tests").select(
            "*, placement_test_emails(*)"
        ).order("created_at", desc=True).limit(limit)
        if domain:
            query = query.eq("domain", domain)
        if status:
            query = query.eq("status", status)
        return query.execute().data
    except Exception:
        raise


_SPAM_TEST_COLS = "eg_test_uuid,sender_email,domain,status,score,score_breakdown,sent_from,sending_server_ip,created_at,completed_at"


@router.get("/deliverability/spam-tests", dependencies=[Security(require_api_key)])
def list_spam_tests(
    domain: Optional[str] = None,
    sender_email: Optional[str] = None,
    limit: int = 50,
):
    supabase = get_supabase()
    try:
        query = supabase.table("spam_filter_tests").select(_SPAM_TEST_COLS).order("created_at", desc=True).limit(limit)
        if domain:
            query = query.eq("domain", domain)
        if sender_email:
            query = query.eq("sender_email", sender_email)
        return query.execute().data
    except Exception:
        raise


# ---------------------------------------------------------------------------
# Placement test schedules — recurring tests run by placement_schedule_runner
# ---------------------------------------------------------------------------

_CADENCE_DELTAS = {
    "daily": timedelta(days=1),
    "weekly": timedelta(weeks=1),
    "monthly": timedelta(days=30),
}


class PlacementScheduleCreate(BaseModel):
    domain: Optional[str] = None
    sender_email: Optional[str] = None
    cadence: Literal["daily", "weekly", "monthly"] = "weekly"
    next_run_at: Optional[str] = Field(
        None, description="ISO timestamp of the first run; defaults to now + one cadence interval."
    )
    enabled: bool = True


class PlacementScheduleUpdate(BaseModel):
    cadence: Optional[Literal["daily", "weekly", "monthly"]] = None
    next_run_at: Optional[str] = None
    enabled: Optional[bool] = None


@router.get("/deliverability/placement-test-schedules", dependencies=[Security(require_api_key)])
def list_placement_test_schedules(enabled: Optional[bool] = None):
    """Recurring placement test schedules with their next run date/time."""
    supabase = get_supabase()
    try:
        query = (
            supabase.table("placement_test_schedules")
            .select("*")
            .order("next_run_at")
        )
        if enabled is not None:
            query = query.eq("enabled", enabled)
        return query.execute().data or []
    except Exception:
        raise


@router.post("/deliverability/placement-test-schedules", dependencies=[Security(require_api_key)])
def create_placement_test_schedule(
    body: PlacementScheduleCreate, x_user_email: str = Header(default=None)
):
    if not body.domain and not body.sender_email:
        raise HTTPException(status_code=422, detail="domain or sender_email is required")
    now = datetime.now(timezone.utc)
    if body.next_run_at:
        try:
            next_run = datetime.fromisoformat(body.next_run_at.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=422, detail="next_run_at must be an ISO timestamp")
    else:
        next_run = now + _CADENCE_DELTAS[body.cadence]
    supabase = get_supabase()
    try:
        row = {
            "domain": body.domain,
            "sender_email": body.sender_email,
            "cadence": body.cadence,
            "enabled": body.enabled,
            "next_run_at": next_run.isoformat(),
            "created_by": x_user_email,
        }
        result = supabase.table("placement_test_schedules").insert(row).execute()
        log_action(
            "create_placement_test_schedule", "domain",
            body.domain or body.sender_email,
            payload=row, performed_by=x_user_email,
        )
        return (result.data or [row])[0]
    except HTTPException:
        raise
    except Exception:
        raise


@router.patch("/deliverability/placement-test-schedules/{schedule_id}", dependencies=[Security(require_api_key)])
def update_placement_test_schedule(
    schedule_id: int, body: PlacementScheduleUpdate, x_user_email: str = Header(default=None)
):
    update = {k: v for k, v in body.dict().items() if v is not None}
    if not update:
        raise HTTPException(status_code=422, detail="No fields to update")
    update["updated_at"] = datetime.now(timezone.utc).isoformat()
    supabase = get_supabase()
    try:
        result = (
            supabase.table("placement_test_schedules")
            .update(update)
            .eq("id", schedule_id)
            .execute()
        )
        if not result.data:
            raise HTTPException(status_code=404, detail="Schedule not found")
        log_action(
            "update_placement_test_schedule", "schedule", str(schedule_id),
            payload=update, performed_by=x_user_email,
        )
        return result.data[0]
    except HTTPException:
        raise
    except Exception:
        raise


@router.delete("/deliverability/placement-test-schedules/{schedule_id}", dependencies=[Security(require_api_key)])
def delete_placement_test_schedule(schedule_id: int, x_user_email: str = Header(default=None)):
    supabase = get_supabase()
    try:
        result = (
            supabase.table("placement_test_schedules")
            .delete()
            .eq("id", schedule_id)
            .execute()
        )
        if not result.data:
            raise HTTPException(status_code=404, detail="Schedule not found")
        log_action(
            "delete_placement_test_schedule", "schedule", str(schedule_id),
            performed_by=x_user_email,
        )
        return {"success": True}
    except HTTPException:
        raise
    except Exception:
        raise


# ---------------------------------------------------------------------------
# InboxAssure placement scores — latest completed result per inbox/domain.
# Populated by workers/inboxassure_poller.py (read-only; never launches tests).
# ---------------------------------------------------------------------------

_IA_COLS = (
    "ia_test_id,domain,inbox_email,status,overall_score,google_score,outlook_score,"
    "inbox_count,spam_count,missing_count,test_completed_at,test_created_at,fetched_at"
)


@router.get("/deliverability/inboxassure", dependencies=[Security(require_api_key)])
def list_inboxassure_results(
    domain: Optional[str] = None,
    inbox_email: Optional[str] = None,
    latest_only: bool = True,
    limit: int = 200,
):
    """
    InboxAssure placement test results. By default returns only the latest
    completed result per inbox/domain; pass latest_only=false for history.
    Also reports whether the integration is configured so the UI can show a
    setup hint instead of an empty table.
    """
    from lib import inboxassure

    supabase = get_supabase()
    try:
        query = (
            supabase.table("inboxassure_placement_results")
            .select(_IA_COLS)
            .order("test_completed_at", desc=True)
            .limit(limit)
        )
        if domain:
            query = query.eq("domain", domain)
        if inbox_email:
            query = query.eq("inbox_email", inbox_email)
        rows = query.execute().data or []
    except Exception as e:
        # Table missing (migration 013 not applied) — treat as unconfigured.
        logger.warning(f"inboxassure_placement_results lookup failed: {e}")
        rows = []

    if latest_only:
        latest: dict[str, dict] = {}
        for row in rows:
            key = row.get("inbox_email") or row.get("domain") or row.get("ia_test_id")
            if key and key not in latest:
                latest[key] = row
        rows = list(latest.values())

    return {"configured": inboxassure.is_configured(), "results": rows}


# ---------------------------------------------------------------------------
# InboxAssure spamcheck.completed runs — ingested via n8n webhook
# (POST /webhooks/inboxassure/spamcheck-completed). Distinct from the
# placement-test poller above.
# ---------------------------------------------------------------------------


@router.get(
    "/deliverability/inboxassure/spamchecks",
    dependencies=[Security(require_api_key)],
)
def list_inboxassure_spamchecks(
    workspace_id: Optional[str] = None,
    limit: int = 50,
):
    """List recent spamcheck runs. Scoped by workspace_id when provided."""
    from lib.inboxassure_spamcheck import list_spamchecks

    try:
        runs = list_spamchecks(workspace_id=workspace_id, limit=limit)
    except Exception as e:
        # Table missing (migration 019 not applied) — empty list for the UI.
        logger.warning(f"inboxassure_spamchecks list failed: {e}")
        runs = []
    return {"spamchecks": runs}


@router.get(
    "/deliverability/inboxassure/spamchecks/{ia_spamcheck_id}",
    dependencies=[Security(require_api_key)],
)
def get_inboxassure_spamcheck(
    ia_spamcheck_id: int,
    workspace_id: Optional[str] = None,
):
    """One spamcheck run with per-account reports.

    Optional workspace_id enforces that the run belongs to the selected
    workspace (404 otherwise). Omit/all skips that check.
    """
    from lib.inboxassure_spamcheck import get_spamcheck

    try:
        run = get_spamcheck(ia_spamcheck_id)
    except Exception as e:
        logger.warning(f"inboxassure_spamchecks get failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to load spamcheck") from e

    if not run:
        raise HTTPException(status_code=404, detail="Spamcheck not found")

    ws = workspace_id if workspace_id and workspace_id != "all" else None
    if ws and run.get("workspace_id") and run["workspace_id"] != ws:
        raise HTTPException(status_code=404, detail="Spamcheck not found")

    return run


_SURBL_COLS = "eg_check_uuid,domain,status,listed,triggered_by,created_at,completed_at"


@router.get("/deliverability/surbl-checks", dependencies=[Security(require_api_key)])
def list_surbl_checks(
    domain: Optional[str] = None,
    listed: Optional[bool] = None,
    limit: int = 50,
):
    supabase = get_supabase()
    try:
        query = supabase.table("surbl_checks").select(_SURBL_COLS).order("created_at", desc=True).limit(limit)
        if domain:
            query = query.eq("domain", domain)
        if listed is not None:
            query = query.eq("listed", listed)
        return query.execute().data
    except Exception:
        raise


