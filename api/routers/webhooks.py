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


@router.post("/webhook")
async def receive_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Webhook-Signature", "")

    if not verify_signature(body, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # EmailGuard sends event as a nested dict: {"event": {"type": "DOMAIN_BLACKLIST_CHECK_PERFORMED", ...}}
    # EmailBison sends it as a flat string: {"event": "email_sent", ...}
    raw_event = payload.get("event") or payload.get("type", "unknown")
    if isinstance(raw_event, dict):
        event_type = raw_event.get("type", "unknown")
    else:
        event_type = raw_event
    logger.info(f"Received webhook event: {event_type}")

    handlers = {
        "email_sent": handle_email_sent,
        "lead_replied": handle_lead_replied,
        "lead_interested": handle_lead_interested,
        "email_bounced": handle_email_bounced,
        "email_account_disconnected": handle_account_disconnected,
        "email_account_reconnected": handle_account_reconnected,
        "DOMAIN_BLACKLIST_CHECK_PERFORMED": handle_domain_blacklist_check,
    }

    handler = handlers.get(event_type)
    if handler:
        try:
            # Handlers do blocking Supabase I/O; run off the event loop.
            await run_in_threadpool(handler, payload)
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


def handle_domain_blacklist_check(payload: dict) -> None:
    check = (payload.get("data") or {}).get("blacklist_check") or {}
    eg_uuid = check.get("uuid", "")
    domain = check.get("domain", "")
    blacklists_count = int(check.get("blacklists_count") or 0)

    if not eg_uuid or not domain:
        logger.warning(f"DOMAIN_BLACKLIST_CHECK_PERFORMED missing uuid/domain: {payload}")
        return

    # Upsert into domain_blacklist_checks so the table stays current in real time
    try:
        get_supabase().table("domain_blacklist_checks").upsert(
            {
                "eg_check_uuid": eg_uuid,
                "domain": domain,
                "ip": check.get("ip"),
                "type": check.get("type"),
                "status": check.get("status"),
                "blacklists_count": blacklists_count,
                "blacklists": check.get("blacklists") or [],
                "last_polled_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="eg_check_uuid",
        ).execute()
        logger.info(f"Upserted domain blacklist check via webhook: {domain} ({blacklists_count} blacklists)")
    except Exception as e:
        logger.error(f"Failed to upsert domain blacklist check from webhook: {e}")

    # Fire a critical notification only if newly blacklisted
    if blacklists_count > 0 and not _notification_exists("domain_blacklisted", eg_uuid):
        blacklists = check.get("blacklists") or []
        create_notification(
            severity="critical",
            type_="domain_blacklisted",
            title=f"Domain blacklisted: {domain}",
            body=f"Found on {blacklists_count} blacklist(s): {', '.join(blacklists)}.",
            entity_type="domain",
            entity_id=eg_uuid,
        )


