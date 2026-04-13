"""
api/main.py — Ottit CRM Action API

Read endpoints serve from Supabase (kept fresh by pollers).
Write/action endpoints proxy to EmailBison/EmailGuard and log to dashboard_action_log.

Architecture:
  Frontend → this API → Supabase (reads) / EmailBison+EmailGuard (writes)
"""

import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Header, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

from lib import emailbison, emailguard, config
from lib.supabase_client import get_supabase
from api.logging_utils import log_action
from lib.notifications import create_notification

app = FastAPI(title="Ottit CRM API", version="1.0.0")

_cors_origins = config.ALLOWED_ORIGINS if config.ALLOWED_ORIGINS else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=bool(config.ALLOWED_ORIGINS),
    allow_methods=["*"],
    allow_headers=["*"],
)

logger = logging.getLogger(__name__)
_bearer = HTTPBearer(auto_error=False)


def require_api_key(credentials: HTTPAuthorizationCredentials = Security(_bearer)):
    """Validate Bearer token. Skipped if API_KEY is unset (dev mode)."""
    if not config.API_KEY:
        return
    if credentials is None or credentials.credentials != config.API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    checks = {}
    try:
        get_supabase().table("notifications").select("id").limit(1).execute()
        checks["supabase"] = "ok"
    except Exception as e:
        checks["supabase"] = f"error: {e}"
    try:
        emailbison.get("/api/sender-emails")
        checks["emailbison"] = "ok"
    except Exception as e:
        checks["emailbison"] = f"error: {e}"
    try:
        emailguard.get("/api/v1/inbox-placement-tests")
        checks["emailguard"] = "ok"
    except Exception as e:
        checks["emailguard"] = f"error: {e}"

    overall = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    return {"status": overall, **checks}


# ---------------------------------------------------------------------------
# Stats overview
# ---------------------------------------------------------------------------

_WORKSPACE_STATS_COLS = "stat_date,emails_sent,emails_opened,emails_replied,emails_bounced,unsubscribed,interested"


@app.get("/stats", dependencies=[Security(require_api_key)])
def get_stats(days: int = 30):
    """
    Workspace-level email stats from workspace_daily_stats (written by stats_poller).
    Returns per-day rows + aggregated totals for the last N days.
    """
    supabase = get_supabase()
    since = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    try:
        result = supabase.table("workspace_daily_stats").select(_WORKSPACE_STATS_COLS).gte(
            "stat_date", since
        ).order("stat_date").execute()
        rows = result.data
        totals = {
            "emails_sent": sum(r.get("emails_sent", 0) or 0 for r in rows),
            "emails_opened": sum(r.get("emails_opened", 0) or 0 for r in rows),
            "emails_replied": sum(r.get("emails_replied", 0) or 0 for r in rows),
            "emails_bounced": sum(r.get("emails_bounced", 0) or 0 for r in rows),
            "unsubscribed": sum(r.get("unsubscribed", 0) or 0 for r in rows),
            "interested": sum(r.get("interested", 0) or 0 for r in rows),
        }
        return {"period_days": days, "totals": totals, "by_date": rows}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Senders — from Supabase
# ---------------------------------------------------------------------------

@app.get("/senders", dependencies=[Security(require_api_key)])
def list_senders(domain: Optional[str] = None, warmup_enabled: Optional[bool] = None):
    """
    Latest stats per sender from Supabase.
    Falls back to most recent available date via get_latest_sender_stats RPC
    if today has no data yet.
    """
    supabase = get_supabase()
    try:
        query = supabase.table("sender_daily_stats").select("*").eq("stat_date", _today())
        if domain:
            query = query.eq("domain", domain)
        if warmup_enabled is not None:
            query = query.eq("warmup_enabled", warmup_enabled)
        result = query.order("sender_email").execute()
        rows = result.data

        if not rows:
            # Fall back: most recent record per sender via DISTINCT ON RPC
            params: dict = {}
            if domain:
                params["p_domain"] = domain
            if warmup_enabled is not None:
                params["p_warmup_enabled"] = warmup_enabled
            rows = supabase.rpc("get_latest_sender_stats", params).execute().data

        return rows
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/senders/{sender_email_id}/history", dependencies=[Security(require_api_key)])
def sender_history(sender_email_id: int, days: int = 30):
    """Time-series stats for a single sender over the last N days."""
    supabase = get_supabase()
    since = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    try:
        result = supabase.table("sender_daily_stats").select("*").eq(
            "sender_email_id", sender_email_id
        ).gte("stat_date", since).order("stat_date").execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Sender not found or no data")
        return result.data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Campaigns — live from EmailBison
# ---------------------------------------------------------------------------

@app.get("/campaigns", dependencies=[Security(require_api_key)])
def list_campaigns(status: Optional[str] = None):
    """
    Live campaigns from EmailBison with real stats.
    Optionally filter by status: active, paused, archived, completed, draft.
    """
    try:
        campaigns = emailbison.get_campaigns()
        normalized = [
            {
                "campaign_id": str(c.get("id")),
                "campaign_name": c.get("name", ""),
                "campaign_status": c.get("status", "unknown"),
                "emails_sent_count": c.get("emails_sent", 0) or 0,
                "reply_count": c.get("replied", 0) or 0,
                "bounced_count": c.get("bounced", 0) or 0,
                "total_leads": c.get("total_leads", 0) or 0,
                "completion_percentage": c.get("completion_percentage", 0) or 0,
                "created_at": c.get("created_at"),
            }
            for c in campaigns
        ]
        if status:
            normalized = [c for c in normalized if c["campaign_status"] == status]
        return normalized
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
# Leads & Replies — live from EmailBison (no Supabase table yet)
# ---------------------------------------------------------------------------

@app.get("/leads", dependencies=[Security(require_api_key)])
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
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/replies", dependencies=[Security(require_api_key)])
def list_replies(campaign_id: Optional[str] = None):
    """Live from EmailBison. Optionally filter by campaign_id."""
    try:
        replies = emailbison.get_replies(campaign_id=campaign_id)
        return [
            {
                "id": r.get("id"),
                "lead_email": r.get("from_email_address") or r.get("lead_email") or r.get("from_email") or r.get("email"),
                "from_name": r.get("from_name"),
                "subject": r.get("subject"),
                "campaign_id": r.get("campaign_id"),
                "body": r.get("text_body") or r.get("body") or r.get("message") or r.get("content"),
                "created_at": r.get("date_received") or r.get("created_at"),
                "interested": r.get("interested") if r.get("interested") is not None else r.get("is_interested"),
            }
            for r in replies
        ]
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
# Deliverability — from Supabase
# ---------------------------------------------------------------------------

@app.get("/deliverability/domain-health", dependencies=[Security(require_api_key)])
def list_domain_health(domain: Optional[str] = None):
    """
    Aggregated domain health from v_domain_health view.
    Includes avg spam score, placement pass/fail counts, last tested date.
    """
    supabase = get_supabase()
    try:
        query = supabase.table("v_domain_health").select("*").order("last_tested_at", desc=True)
        if domain:
            query = query.eq("domain", domain)
        return query.execute().data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/deliverability/placement-tests", dependencies=[Security(require_api_key)])
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


_SPAM_TEST_COLS = "eg_test_uuid,sender_email,domain,status,score,score_breakdown,sent_from,sending_server_ip,created_at,completed_at"


@app.get("/deliverability/spam-tests", dependencies=[Security(require_api_key)])
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


_SURBL_COLS = "eg_check_uuid,domain,status,listed,triggered_by,created_at,completed_at"


@app.get("/deliverability/surbl-checks", dependencies=[Security(require_api_key)])
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Notifications — from Supabase
# ---------------------------------------------------------------------------

_NOTIFICATION_COLS = "id,severity,type,entity_type,entity_id,title,body,read,resolved,created_at"


@app.get("/notifications", dependencies=[Security(require_api_key)])
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/notifications/{notification_id}/read", dependencies=[Security(require_api_key)])
def mark_notification_read(notification_id: int):
    supabase = get_supabase()
    try:
        result = supabase.table("notifications").update({"read": True}).eq("id", notification_id).execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Notification not found")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/notifications/{notification_id}/resolve", dependencies=[Security(require_api_key)])
def resolve_notification(notification_id: int):
    supabase = get_supabase()
    try:
        result = supabase.table("notifications").update({"resolved": True, "read": True}).eq("id", notification_id).execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Notification not found")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/notifications/read-all", dependencies=[Security(require_api_key)])
def mark_all_notifications_read():
    supabase = get_supabase()
    try:
        supabase.table("notifications").update({"read": True}).eq("read", False).execute()
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

def verify_signature(body: bytes, signature: str) -> bool:
    if not config.WEBHOOK_SECRET:
        return True
    expected = hmac.HMAC(
        config.WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _notification_exists(type_: str, entity_id: str) -> bool:
    try:
        result = (
            get_supabase()
            .table("notifications")
            .select("id")
            .eq("type", type_)
            .eq("entity_id", entity_id)
            .gte("created_at", _today())
            .limit(1)
            .execute()
        )
        return len(result.data) > 0
    except Exception:
        return False


@app.post("/webhook")
async def receive_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Webhook-Signature", "")

    if not verify_signature(body, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = payload.get("event") or payload.get("type", "unknown")
    logger.info(f"Received webhook event: {event_type}")

    handlers = {
        "email_sent": handle_email_sent,
        "lead_replied": handle_lead_replied,
        "lead_interested": handle_lead_interested,
        "email_bounced": handle_email_bounced,
        "email_account_disconnected": handle_account_disconnected,
        "email_account_reconnected": handle_account_reconnected,
    }

    handler = handlers.get(event_type)
    if handler:
        try:
            handler(payload)
        except Exception as e:
            logger.error(f"Handler for {event_type} failed: {e}")

    return {"received": True, "event": event_type}


def handle_email_sent(payload: dict) -> None:
    logger.debug("email_sent: counters updated by stats_poller on next run")


def handle_lead_replied(payload: dict) -> None:
    entity_id = str(payload.get("lead_id", ""))
    if _notification_exists("lead_replied", entity_id):
        return
    create_notification(
        severity="info", type_="lead_replied",
        title="New lead reply", body="Lead replied to campaign.",
        entity_type="lead", entity_id=entity_id,
    )


def handle_lead_interested(payload: dict) -> None:
    entity_id = str(payload.get("lead_id", ""))
    if _notification_exists("lead_interested", entity_id):
        return
    create_notification(
        severity="info", type_="lead_interested",
        title="Lead marked as interested", body="A lead has shown interest.",
        entity_type="lead", entity_id=entity_id,
    )


def handle_email_bounced(payload: dict) -> None:
    logger.info(f"email_bounced: {payload.get('email', 'unknown')}")


def handle_account_disconnected(payload: dict) -> None:
    account = payload.get("email_account", {})
    entity_id = str(account.get("id", ""))
    if _notification_exists("account_disconnected", entity_id):
        return
    create_notification(
        severity="critical", type_="account_disconnected",
        title=f"Sender disconnected: {account.get('email', 'unknown')}",
        body="Email account was disconnected. Campaigns may be paused.",
        entity_type="sender", entity_id=entity_id,
    )


def handle_account_reconnected(payload: dict) -> None:
    account = payload.get("email_account", {})
    entity_id = str(account.get("id", ""))
    if _notification_exists("account_reconnected", entity_id):
        return
    create_notification(
        severity="resolved", type_="account_reconnected",
        title=f"Sender reconnected: {account.get('email', 'unknown')}",
        body="Email account reconnected successfully.",
        entity_type="sender", entity_id=entity_id,
    )


# ---------------------------------------------------------------------------
# Campaign actions
# ---------------------------------------------------------------------------

@app.post("/actions/campaigns/{campaign_id}/pause", dependencies=[Security(require_api_key)])
async def pause_campaign(campaign_id: str, x_user_email: str = Header(default=None)):
    try:
        result = emailbison.patch(f"/api/campaigns/{campaign_id}/pause")
        log_action("campaign_pause", "campaign", campaign_id, payload={"campaign_id": campaign_id}, api_response=result, performed_by=x_user_email)
        return {"success": True, "data": result}
    except Exception as e:
        log_action("campaign_pause", "campaign", campaign_id, status="error", error_message=str(e), performed_by=x_user_email)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/actions/campaigns/{campaign_id}/resume", dependencies=[Security(require_api_key)])
async def resume_campaign(campaign_id: str, x_user_email: str = Header(default=None)):
    try:
        result = emailbison.patch(f"/api/campaigns/{campaign_id}/resume")
        log_action("campaign_resume", "campaign", campaign_id, payload={"campaign_id": campaign_id}, api_response=result, performed_by=x_user_email)
        return {"success": True, "data": result}
    except Exception as e:
        log_action("campaign_resume", "campaign", campaign_id, status="error", error_message=str(e), performed_by=x_user_email)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/actions/campaigns/{campaign_id}/archive", dependencies=[Security(require_api_key)])
async def archive_campaign(campaign_id: str, x_user_email: str = Header(default=None)):
    try:
        result = emailbison.patch(f"/api/campaigns/{campaign_id}/archive")
        log_action("campaign_archive", "campaign", campaign_id, payload={"campaign_id": campaign_id}, api_response=result, performed_by=x_user_email)
        return {"success": True, "data": result}
    except Exception as e:
        log_action("campaign_archive", "campaign", campaign_id, status="error", error_message=str(e), performed_by=x_user_email)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/actions/campaigns/{campaign_id}/duplicate", dependencies=[Security(require_api_key)])
async def duplicate_campaign(campaign_id: str, x_user_email: str = Header(default=None)):
    try:
        result = emailbison.post(f"/api/campaigns/{campaign_id}/duplicate")
        log_action("campaign_duplicate", "campaign", campaign_id, payload={"campaign_id": campaign_id}, api_response=result, performed_by=x_user_email)
        return {"success": True, "data": result}
    except Exception as e:
        log_action("campaign_duplicate", "campaign", campaign_id, status="error", error_message=str(e), performed_by=x_user_email)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Sender actions
# ---------------------------------------------------------------------------

class UpdateDailyLimitRequest(BaseModel):
    daily_limit: int


@app.patch("/actions/senders/{sender_id}/daily-limit", dependencies=[Security(require_api_key)])
async def update_daily_limit(sender_id: str, body: UpdateDailyLimitRequest, x_user_email: str = Header(default=None)):
    try:
        result = emailbison.patch(f"/api/sender-emails/{sender_id}", {"daily_limit": body.daily_limit})
        log_action("sender_update_daily_limit", "sender", sender_id, payload=body.dict(), api_response=result, performed_by=x_user_email)
        return {"success": True, "data": result}
    except Exception as e:
        log_action("sender_update_daily_limit", "sender", sender_id, status="error", error_message=str(e), performed_by=x_user_email)
        raise HTTPException(status_code=500, detail=str(e))


class ToggleWarmupRequest(BaseModel):
    enable: bool


@app.patch("/actions/senders/{sender_id}/warmup", dependencies=[Security(require_api_key)])
async def toggle_warmup(sender_id: str, body: ToggleWarmupRequest, x_user_email: str = Header(default=None)):
    path = "/api/warmup/sender-emails/enable" if body.enable else "/api/warmup/sender-emails/disable"
    try:
        result = emailbison.patch(path, {"sender_email_ids": [sender_id]})
        log_action("sender_toggle_warmup", "sender", sender_id, payload=body.dict(), api_response=result, performed_by=x_user_email)
        return {"success": True, "data": result}
    except Exception as e:
        log_action("sender_toggle_warmup", "sender", sender_id, status="error", error_message=str(e), performed_by=x_user_email)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Reply actions
# ---------------------------------------------------------------------------

@app.post("/actions/replies/{reply_id}/mark-interested", dependencies=[Security(require_api_key)])
async def mark_interested(reply_id: str, x_user_email: str = Header(default=None)):
    try:
        result = emailbison.patch(f"/api/replies/{reply_id}/interested")
        log_action("reply_mark_interested", "reply", reply_id, api_response=result, performed_by=x_user_email)
        return {"success": True}
    except Exception as e:
        log_action("reply_mark_interested", "reply", reply_id, status="error", error_message=str(e), performed_by=x_user_email)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/actions/replies/{reply_id}/mark-not-interested", dependencies=[Security(require_api_key)])
async def mark_not_interested(reply_id: str, x_user_email: str = Header(default=None)):
    try:
        result = emailbison.patch(f"/api/replies/{reply_id}/uninterested")
        log_action("reply_mark_not_interested", "reply", reply_id, api_response=result, performed_by=x_user_email)
        return {"success": True}
    except Exception as e:
        log_action("reply_mark_not_interested", "reply", reply_id, status="error", error_message=str(e), performed_by=x_user_email)
        raise HTTPException(status_code=500, detail=str(e))


class SendReplyRequest(BaseModel):
    to: str
    subject: str
    body: str
    campaign_id: Optional[str] = None


@app.post("/actions/replies/send", dependencies=[Security(require_api_key)])
async def send_reply(body: SendReplyRequest, x_user_email: str = Header(default=None)):
    try:
        result = emailbison.post("/api/replies/new", body.dict())
        log_action("reply_send", "reply", None, payload={"to": body.to, "subject": body.subject}, api_response=result, performed_by=x_user_email)
        return {"success": True, "data": result}
    except Exception as e:
        log_action("reply_send", "reply", None, status="error", error_message=str(e), performed_by=x_user_email)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Deliverability actions
# ---------------------------------------------------------------------------

class TriggerTestRequest(BaseModel):
    domain: Optional[str] = None
    sender_email: Optional[str] = None


@app.post("/actions/deliverability/placement-test", dependencies=[Security(require_api_key)])
async def trigger_placement_test(body: TriggerTestRequest, x_user_email: str = Header(default=None)):
    try:
        result = emailguard.post("/api/v1/inbox-placement-tests", body.dict(exclude_none=True))
        log_action("trigger_placement_test", "domain", body.domain, payload=body.dict(), api_response=result, performed_by=x_user_email)
        return {"success": True, "data": result}
    except Exception as e:
        log_action("trigger_placement_test", "domain", body.domain, status="error", error_message=str(e), performed_by=x_user_email)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/actions/deliverability/spam-test", dependencies=[Security(require_api_key)])
async def trigger_spam_test(body: TriggerTestRequest, x_user_email: str = Header(default=None)):
    try:
        result = emailguard.post("/api/v1/spam-filter-tests", body.dict(exclude_none=True))
        log_action("trigger_spam_test", "domain", body.domain, payload=body.dict(), api_response=result, performed_by=x_user_email)
        return {"success": True, "data": result}
    except Exception as e:
        log_action("trigger_spam_test", "domain", body.domain, status="error", error_message=str(e), performed_by=x_user_email)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/actions/deliverability/surbl-check", dependencies=[Security(require_api_key)])
async def trigger_surbl_check(body: TriggerTestRequest, x_user_email: str = Header(default=None)):
    try:
        result = emailguard.post("/api/v1/surbl-blacklist-checks/domains", body.dict(exclude_none=True))
        log_action("trigger_surbl_check", "domain", body.domain, payload=body.dict(), api_response=result, performed_by=x_user_email)
        return {"success": True, "data": result}
    except Exception as e:
        log_action("trigger_surbl_check", "domain", body.domain, status="error", error_message=str(e), performed_by=x_user_email)
        raise HTTPException(status_code=500, detail=str(e))
