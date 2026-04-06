"""
api/main.py — Ottit CRM Action API

All dashboard write operations (actions) come through here.
The frontend NEVER calls EmailBison or EmailGuard directly.

Architecture:
  Frontend → this API → EmailBison/EmailGuard → log to dashboard_action_log
"""

import hashlib
import hmac
import json
import logging
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from lib import emailbison, emailguard, config
from lib.supabase_client import get_supabase
from api.logging_utils import log_action
from workers.notifier import _create_notification

app = FastAPI(title="Ottit CRM API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Webhook (preserved from workers/webhook_receiver.py)
# ---------------------------------------------------------------------------

def verify_signature(body: bytes, signature: str) -> bool:
    """Verify HMAC signature if WEBHOOK_SECRET is configured."""
    if not config.WEBHOOK_SECRET:
        return True  # Skip verification if no secret configured
    expected = hmac.HMAC(
        config.WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


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
    logger.debug("email_sent: updating counters via stats poller next run")


def handle_lead_replied(payload: dict) -> None:
    _create_notification(
        severity="info",
        type_="lead_replied",
        title="New lead reply",
        body="Lead replied to campaign.",
        entity_type="lead",
        entity_id=str(payload.get("lead_id", "")),
    )


def handle_lead_interested(payload: dict) -> None:
    _create_notification(
        severity="info",
        type_="lead_interested",
        title="Lead marked as interested",
        body="A lead has shown interest.",
        entity_type="lead",
        entity_id=str(payload.get("lead_id", "")),
    )


def handle_email_bounced(payload: dict) -> None:
    logger.info(f"email_bounced: {payload.get('email', 'unknown')}")


def handle_account_disconnected(payload: dict) -> None:
    account = payload.get("email_account", {})
    email = account.get("email", "unknown")
    _create_notification(
        severity="critical",
        type_="account_disconnected",
        title=f"Sender disconnected: {email}",
        body="Email account was disconnected. Campaigns may be paused.",
        entity_type="sender",
        entity_id=str(account.get("id", "")),
    )


def handle_account_reconnected(payload: dict) -> None:
    account = payload.get("email_account", {})
    email = account.get("email", "unknown")
    _create_notification(
        severity="resolved",
        type_="account_reconnected",
        title=f"Sender reconnected: {email}",
        body="Email account reconnected successfully.",
        entity_type="sender",
        entity_id=str(account.get("id", "")),
    )


# ---------------------------------------------------------------------------
# Campaign actions
# ---------------------------------------------------------------------------

@app.post("/actions/campaigns/{campaign_id}/pause")
async def pause_campaign(campaign_id: str, x_user_email: str = Header(default=None)):
    try:
        result = emailbison.patch(f"/api/campaigns/{campaign_id}/pause")
        log_action("campaign_pause", "campaign", campaign_id, payload={"campaign_id": campaign_id}, api_response=result, performed_by=x_user_email)
        return {"success": True, "data": result}
    except Exception as e:
        log_action("campaign_pause", "campaign", campaign_id, status="error", error_message=str(e), performed_by=x_user_email)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/actions/campaigns/{campaign_id}/resume")
async def resume_campaign(campaign_id: str, x_user_email: str = Header(default=None)):
    try:
        result = emailbison.patch(f"/api/campaigns/{campaign_id}/resume")
        log_action("campaign_resume", "campaign", campaign_id, payload={"campaign_id": campaign_id}, api_response=result, performed_by=x_user_email)
        return {"success": True, "data": result}
    except Exception as e:
        log_action("campaign_resume", "campaign", campaign_id, status="error", error_message=str(e), performed_by=x_user_email)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/actions/campaigns/{campaign_id}/archive")
async def archive_campaign(campaign_id: str, x_user_email: str = Header(default=None)):
    try:
        result = emailbison.patch(f"/api/campaigns/{campaign_id}/archive")
        log_action("campaign_archive", "campaign", campaign_id, payload={"campaign_id": campaign_id}, api_response=result, performed_by=x_user_email)
        return {"success": True, "data": result}
    except Exception as e:
        log_action("campaign_archive", "campaign", campaign_id, status="error", error_message=str(e), performed_by=x_user_email)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/actions/campaigns/{campaign_id}/duplicate")
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


@app.patch("/actions/senders/{sender_id}/daily-limit")
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


@app.patch("/actions/senders/{sender_id}/warmup")
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

@app.post("/actions/replies/{reply_id}/mark-interested")
async def mark_interested(reply_id: str, x_user_email: str = Header(default=None)):
    try:
        result = emailbison.patch(f"/api/replies/{reply_id}/interested")
        log_action("reply_mark_interested", "reply", reply_id, api_response=result, performed_by=x_user_email)
        return {"success": True}
    except Exception as e:
        log_action("reply_mark_interested", "reply", reply_id, status="error", error_message=str(e), performed_by=x_user_email)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/actions/replies/{reply_id}/mark-not-interested")
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


@app.post("/actions/replies/send")
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


@app.post("/actions/deliverability/placement-test")
async def trigger_placement_test(body: TriggerTestRequest, x_user_email: str = Header(default=None)):
    try:
        result = emailguard.post("/api/v1/inbox-placement-tests", body.dict(exclude_none=True))
        log_action("trigger_placement_test", "domain", body.domain, payload=body.dict(), api_response=result, performed_by=x_user_email)
        return {"success": True, "data": result}
    except Exception as e:
        log_action("trigger_placement_test", "domain", body.domain, status="error", error_message=str(e), performed_by=x_user_email)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/actions/deliverability/spam-test")
async def trigger_spam_test(body: TriggerTestRequest, x_user_email: str = Header(default=None)):
    try:
        result = emailguard.post("/api/v1/spam-filter-tests", body.dict(exclude_none=True))
        log_action("trigger_spam_test", "domain", body.domain, payload=body.dict(), api_response=result, performed_by=x_user_email)
        return {"success": True, "data": result}
    except Exception as e:
        log_action("trigger_spam_test", "domain", body.domain, status="error", error_message=str(e), performed_by=x_user_email)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/actions/deliverability/surbl-check")
async def trigger_surbl_check(body: TriggerTestRequest, x_user_email: str = Header(default=None)):
    try:
        result = emailguard.post("/api/v1/surbl-blacklist-checks/domains", body.dict(exclude_none=True))
        log_action("trigger_surbl_check", "domain", body.domain, payload=body.dict(), api_response=result, performed_by=x_user_email)
        return {"success": True, "data": result}
    except Exception as e:
        log_action("trigger_surbl_check", "domain", body.domain, status="error", error_message=str(e), performed_by=x_user_email)
        raise HTTPException(status_code=500, detail=str(e))
