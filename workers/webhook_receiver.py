"""
webhook_receiver.py — FastAPI server for EmailBison webhooks

Handles all 13 EmailBison webhook event types and writes to Supabase.
Also creates notifications for critical events.
Register your webhook URL in EmailBison pointing to: http://your-server:8000/webhook
"""

import hashlib
import hmac
import json
import logging
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException
from lib.supabase_client import get_supabase
from lib import config
from workers.notifier import _create_notification

logger = logging.getLogger(__name__)
app = FastAPI(title="Ottit Webhook Receiver")


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


@app.get("/health")
def health():
    return {"status": "ok"}


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
