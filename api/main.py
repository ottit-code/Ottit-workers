"""
api/main.py — Ottit CRM Action API

Read endpoints serve from Supabase (kept fresh by pollers).
Write/action endpoints proxy to EmailBison/EmailGuard and log to dashboard_action_log.

Architecture:
  Frontend → this API → Supabase (reads) / EmailBison+EmailGuard (writes)

Read endpoint summary
─────────────────────
  GET /health
  GET /stats                        workspace daily stats (incl. prior_totals)
  GET /senders                      sender daily stats
  GET /senders/{id}/history
  GET /campaigns                    live from EmailBison (incl. opened/interested counts)
  GET /leads                        live from EmailBison
  GET /replies                      live from EmailBison + persisted review state
  GET /campaign-stats               campaign_daily_stats (polled)
  GET /campaign-stats/{id}/history
  GET /ab-tests                     ab_test_snapshots (polled)
  GET /ab-tests/{campaign_id}
  GET /reply-events                 reply_events (polled)
  GET /lead-engagement              lead_engagement_snapshots (polled)
  GET /lead-engagement/{lead_id}
  GET /sender-performance           sender_email_performance + warm_state
  GET /sender-performance/{id}
  GET /deliverability/*             domain-health merges SPF/DKIM/DMARC booleans
  GET /notifications
  GET /counts                       aggregated sidebar counts (5s TTL cache)
  GET /lead-pipeline-summary        mutually-exclusive funnel counts
  GET /activity-feed                unified timeline (notifications + replies + DNS)
  GET /workspaces/me                singleton workspace from env config
  GET /workspaces/me/quota          monthly send quota
"""

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import FastAPI, Request, HTTPException, Header, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from lib import emailbison, emailguard, config
from lib.supabase_client import get_supabase
from api.logging_utils import log_action
from lib.notifications import create_notification
from api.routers.drafter_inbound import router as drafter_inbound_router
from api.routers.drafter_admin import router as drafter_admin_router

app = FastAPI(title="Ottit CRM API", version="1.0.0")
app.include_router(drafter_inbound_router)
app.include_router(drafter_admin_router)

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


@app.get("/health")
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


# ---------------------------------------------------------------------------
# Stats overview
# ---------------------------------------------------------------------------

_WORKSPACE_STATS_COLS = "stat_date,emails_sent,emails_opened,emails_replied,emails_bounced,unsubscribed,interested"
_STATS_METRIC_KEYS = ("emails_sent", "emails_opened", "emails_replied", "emails_bounced", "unsubscribed", "interested")


def _sum_metrics(rows: list[dict]) -> dict:
    return {k: sum((r.get(k) or 0) for r in rows) for k in _STATS_METRIC_KEYS}


class StatTotals(BaseModel):
    emails_sent: int
    emails_opened: int
    emails_replied: int
    emails_bounced: int
    unsubscribed: int
    interested: int


class StatsResponse(BaseModel):
    period_days: int
    totals: StatTotals
    by_date: List[dict]
    prior_totals: StatTotals = Field(
        description="Aggregates covering the window of the same length immediately prior to the current period."
    )
    prior_period_days: int


@app.get("/stats", dependencies=[Security(require_api_key)], response_model=StatsResponse)
def get_stats(days: int = 30):
    """
    Workspace-level email stats from workspace_daily_stats (written by stats_poller).
    Returns per-day rows + aggregated totals for the last N days plus prior-period totals
    (same length window immediately before) for delta computation on the client.
    """
    supabase = get_supabase()
    today = datetime.now(timezone.utc).date()
    since = (today - timedelta(days=days)).isoformat()
    prior_since = (today - timedelta(days=days * 2)).isoformat()
    prior_until = since
    try:
        current = supabase.table("workspace_daily_stats").select(_WORKSPACE_STATS_COLS).gte(
            "stat_date", since
        ).order("stat_date").execute().data
        prior = supabase.table("workspace_daily_stats").select(_WORKSPACE_STATS_COLS).gte(
            "stat_date", prior_since
        ).lt("stat_date", prior_until).execute().data
        return {
            "period_days": days,
            "totals": _sum_metrics(current),
            "by_date": current,
            "prior_totals": _sum_metrics(prior),
            "prior_period_days": days,
        }
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

class Campaign(BaseModel):
    campaign_id: str
    campaign_name: str
    campaign_status: str
    emails_sent_count: int
    reply_count: int
    bounced_count: int
    total_leads: int
    completion_percentage: float
    created_at: Optional[str] = None
    opened_count: int = Field(0, description="Total opens (not unique) from EmailBison.")
    unique_opens_count: int = Field(0, description="Unique opens from EmailBison.")
    interested_count: int = Field(0, description="Leads marked interested.")
    booked_count: int = Field(0, description="Leads with a confirmed meeting. Equals interested_count until calendar integration ships.")
    last_sent_at: Optional[str] = Field(None, description="Timestamp of most recent send activity on the campaign (EmailBison updated_at).")


def _normalize_campaign(c: dict) -> dict:
    interested = int(c.get("interested") or 0)
    return {
        "campaign_id": str(c.get("id")),
        "campaign_name": c.get("name", ""),
        "campaign_status": str(c.get("status") or "unknown").lower(),
        "emails_sent_count": int(c.get("emails_sent") or 0),
        "reply_count": int(c.get("replied") or 0),
        "bounced_count": int(c.get("bounced") or 0),
        "total_leads": int(c.get("total_leads") or 0),
        "completion_percentage": float(c.get("completion_percentage") or 0),
        "created_at": c.get("created_at"),
        "opened_count": int(c.get("opened") or 0),
        "unique_opens_count": int(c.get("unique_opens") or 0),
        "interested_count": interested,
        "booked_count": interested,
        "last_sent_at": c.get("updated_at"),
    }


@app.get("/campaigns", dependencies=[Security(require_api_key)], response_model=List[Campaign])
def list_campaigns(status: Optional[str] = None):
    """
    Live campaigns from EmailBison with real stats.
    Optionally filter by status: active, paused, archived, completed, draft.
    """
    try:
        campaigns = emailbison.get_campaigns()
        normalized = [_normalize_campaign(c) for c in campaigns]
        if status:
            wanted = status.lower()
            normalized = [c for c in normalized if c["campaign_status"] == wanted]
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


@app.get("/replies", dependencies=[Security(require_api_key)], response_model=List[Reply])
def list_replies(campaign_id: Optional[str] = None):
    """Live from EmailBison merged with persisted review state from reply_review_state."""
    try:
        replies = emailbison.get_replies(campaign_id=campaign_id)
        reply_ids = [str(r.get("id")) for r in replies if r.get("id") is not None]
        states = _fetch_review_states(reply_ids)
        return [_normalize_reply(r, states.get(str(r.get("id")))) for r in replies]
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


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


@app.get("/deliverability/domain-health", dependencies=[Security(require_api_key)])
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


# ---------------------------------------------------------------------------
# Campaign daily stats — from Supabase (written by campaign_daily_stats_poller)
# ---------------------------------------------------------------------------

_CAMPAIGN_STATS_COLS = (
    "campaign_id,campaign_name,campaign_status,stat_date,"
    "emails_sent,emails_opened,unique_opens,emails_replied,emails_bounced,"
    "unsubscribed,interested,open_rate,reply_rate,bounce_rate,"
    "max_emails_per_day,max_new_leads_per_day,total_leads_contacted,"
    "completion_pct,fetched_at"
)


@app.get("/campaign-stats", dependencies=[Security(require_api_key)])
def list_campaign_stats(
    campaign_id: Optional[str] = None,
    days: int = 30,
    status: Optional[str] = None,
):
    """
    Per-campaign daily stats from campaign_daily_stats (polled daily at midnight).
    Returns rows for the last N days. Filter by campaign_id or status.
    """
    supabase = get_supabase()
    since = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    try:
        query = (
            supabase.table("campaign_daily_stats")
            .select(_CAMPAIGN_STATS_COLS)
            .gte("stat_date", since)
            .order("stat_date", desc=True)
        )
        if campaign_id:
            query = query.eq("campaign_id", campaign_id)
        if status:
            query = query.eq("campaign_status", status)
        return query.execute().data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/campaign-stats/{campaign_id}/history", dependencies=[Security(require_api_key)])
def campaign_stats_history(campaign_id: str, days: int = 90):
    """Full date-series for a single campaign."""
    supabase = get_supabase()
    since = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    try:
        result = (
            supabase.table("campaign_daily_stats")
            .select(_CAMPAIGN_STATS_COLS)
            .eq("campaign_id", campaign_id)
            .gte("stat_date", since)
            .order("stat_date")
            .execute()
        )
        if not result.data:
            raise HTTPException(status_code=404, detail="No stats found for this campaign")
        return result.data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# A/B test snapshots — from Supabase (written by ab_test_snapshots_poller)
# ---------------------------------------------------------------------------

_AB_TEST_COLS = (
    "campaign_id,sequence_step_id,snapshot_date,email_subject,step_order,"
    "is_variant,variant_from_step_id,thread_reply,"
    "emails_sent,opens,unique_opens,clicks,replies,unique_replies,interested,bounced,"
    "open_rate,reply_rate,click_rate,interest_rate,bounce_rate,"
    "stat_confidence,stat_winner,stat_sample_sufficient,fetched_at"
)


@app.get("/ab-tests", dependencies=[Security(require_api_key)])
def list_ab_tests(
    campaign_id: Optional[str] = None,
    snapshot_date: Optional[str] = None,
    variants_only: Optional[bool] = None,
):
    """
    A/B test snapshots from ab_test_snapshots (polled every 6 hours).
    Defaults to today's snapshot. Use snapshot_date=YYYY-MM-DD for a specific date.
    Set variants_only=true to return only variant steps.
    """
    supabase = get_supabase()
    date = snapshot_date or _today()
    try:
        query = (
            supabase.table("ab_test_snapshots")
            .select(_AB_TEST_COLS)
            .eq("snapshot_date", date)
            .order("campaign_id")
            .order("step_order")
        )
        if campaign_id:
            query = query.eq("campaign_id", campaign_id)
        if variants_only is not None:
            query = query.eq("is_variant", variants_only)
        return query.execute().data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/ab-tests/{campaign_id}", dependencies=[Security(require_api_key)])
def get_ab_test_for_campaign(campaign_id: str, snapshot_date: Optional[str] = None):
    """All sequence steps + A/B stats for a specific campaign (today by default)."""
    supabase = get_supabase()
    date = snapshot_date or _today()
    try:
        result = (
            supabase.table("ab_test_snapshots")
            .select(_AB_TEST_COLS)
            .eq("campaign_id", campaign_id)
            .eq("snapshot_date", date)
            .order("step_order")
            .execute()
        )
        if not result.data:
            raise HTTPException(status_code=404, detail="No snapshot found for this campaign")
        return result.data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Reply events — from Supabase (written by reply_events_poller)
# ---------------------------------------------------------------------------

_REPLY_EVENT_COLS = (
    "reply_id,campaign_id,campaign_name,lead_id,lead_email,"
    "sender_email_id,sender_email,sequence_step_id,classification,"
    "folder,replied_at,original_sent_at,response_time_hours,"
    "subject,has_attachment,is_thread_reply,fetched_at"
)


@app.get("/reply-events", dependencies=[Security(require_api_key)])
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Lead engagement snapshots — from Supabase (written by lead_engagement_poller)
# ---------------------------------------------------------------------------

_LEAD_ENGAGEMENT_COLS = (
    "lead_id,snapshot_date,first_name,last_name,email,title,company,"
    "status,tags,emails_sent,opens,unique_opens,replies,unique_replies,"
    "engagement_score,funnel_stage,campaign_engagements,custom_variables,fetched_at"
)


@app.get("/lead-engagement", dependencies=[Security(require_api_key)])
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/lead-engagement/{lead_id}", dependencies=[Security(require_api_key)])
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Sender email performance — from Supabase (written by sender_performance_poller)
# ---------------------------------------------------------------------------

_SENDER_PERF_COLS = (
    "sender_email_id,snapshot_date,sender_email,domain,connection_type,connection_status,"
    "warmup_enabled,emails_sent_count,total_leads_contacted_count,unique_replied_count,"
    "unique_opened_count,bounced_count,interested_leads_count,"
    "reply_rate,open_rate,bounce_rate,interest_rate,"
    "warmup_score,in_recovery,recovery_policy_key,recovery_strike_count,"
    "latest_placement_score,latest_spam_score,health_score,tags,fetched_at"
)

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


def _today_volume_map(sender_ids: list[int]) -> dict[int, int]:
    if not sender_ids:
        return {}
    try:
        rows = (
            get_supabase()
            .table("sender_daily_stats")
            .select("sender_email_id,emails_sent,daily_limit")
            .eq("stat_date", _today())
            .in_("sender_email_id", sender_ids)
            .execute()
            .data or []
        )
        return {int(r["sender_email_id"]): r for r in rows}
    except Exception as e:
        logger.warning(f"sender_daily_stats today lookup failed: {e}")
        return {}


def _warm_state_since_map(sender_ids: list[int]) -> dict[int, Optional[str]]:
    """Batch-compute warm_state_since for many senders in a single query.

    Returns {sender_email_id: timestamp_of_oldest_consecutive_same-state_row_or_None}.
    Replaces N-sequential Supabase round-trips (the previous per-sender helper
    made /sender-performance hang on ~90 senders).
    """
    if not sender_ids:
        return {}
    try:
        rows = (
            get_supabase()
            .table("sender_email_performance")
            .select("sender_email_id,snapshot_date,warmup_score,in_recovery,fetched_at")
            .in_("sender_email_id", sender_ids)
            .order("sender_email_id")
            .order("snapshot_date", desc=True)
            .execute()
            .data or []
        )
    except Exception as e:
        logger.debug(f"warm_state batch lookup failed: {e}")
        return {sid: None for sid in sender_ids}

    by_sender: dict[int, list[dict]] = {}
    for r in rows:
        sid = r.get("sender_email_id")
        if sid is None:
            continue
        by_sender.setdefault(int(sid), []).append(r)

    out: dict[int, Optional[str]] = {}
    for sid in sender_ids:
        history = by_sender.get(sid, [])
        if not history:
            out[sid] = None
            continue
        current_state = _compute_warm_state(history[0])
        last_same = history[0]
        for row in history[1:]:
            if _compute_warm_state(row) == current_state:
                last_same = row
            else:
                break
        out[sid] = last_same.get("fetched_at") or last_same.get("snapshot_date")
    return out


def _effective_daily_limit(row: dict, daily_limit: Optional[int]) -> Optional[int]:
    if daily_limit is None:
        return None
    if not row.get("in_recovery"):
        return daily_limit
    strikes = int(row.get("recovery_strike_count") or 0)
    factor = max(0.25, 1.0 - 0.25 * strikes)
    return int(daily_limit * factor)


def _enrich_sender_perf(rows: list[dict], history: bool = False) -> list[dict]:
    sender_ids = [int(r["sender_email_id"]) for r in rows if r.get("sender_email_id") is not None]
    volume_map = _today_volume_map(sender_ids) if not history else {}
    since_map = _warm_state_since_map(sender_ids) if not history else {}
    enriched: list[dict] = []
    for row in rows:
        state = _compute_warm_state(row)
        sid = int(row["sender_email_id"]) if row.get("sender_email_id") is not None else None
        vol_row = volume_map.get(sid, {}) if sid is not None else {}
        daily_limit = vol_row.get("daily_limit")
        out = dict(row)
        out["warm_state"] = state
        out["warm_state_since"] = since_map.get(sid) if sid is not None and not history else None
        out["daily_volume_today"] = int(vol_row.get("emails_sent") or 0) if not history else None
        out["daily_limit_effective"] = _effective_daily_limit(row, daily_limit)
        enriched.append(out)
    return enriched


@app.get("/sender-performance", dependencies=[Security(require_api_key)])
def list_sender_performance(
    domain: Optional[str] = None,
    in_recovery: Optional[bool] = None,
    snapshot_date: Optional[str] = None,
):
    """
    Sender performance snapshots from sender_email_performance (polled daily at 1 AM).
    Defaults to today's snapshot. Filter by domain or in_recovery status.
    Each row includes warm_state, warm_state_since, daily_volume_today, daily_limit_effective.
    """
    supabase = get_supabase()
    date = snapshot_date or _today()
    try:
        query = (
            supabase.table("sender_email_performance")
            .select(_SENDER_PERF_COLS)
            .eq("snapshot_date", date)
            .order("health_score", desc=True)
        )
        if domain:
            query = query.eq("domain", domain)
        if in_recovery is not None:
            query = query.eq("in_recovery", in_recovery)
        return _enrich_sender_perf(query.execute().data or [])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/sender-performance/{sender_email_id}", dependencies=[Security(require_api_key)])
def get_sender_performance_history(sender_email_id: int, days: int = 30):
    """Time-series performance snapshots for a single sender."""
    supabase = get_supabase()
    since = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    try:
        result = (
            supabase.table("sender_email_performance")
            .select(_SENDER_PERF_COLS)
            .eq("sender_email_id", sender_email_id)
            .gte("snapshot_date", since)
            .order("snapshot_date", desc=True)
            .execute()
        )
        if not result.data:
            raise HTTPException(status_code=404, detail="No performance data for this sender")
        return _enrich_sender_perf(result.data, history=True)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Aggregates — /counts, /lead-pipeline-summary, /activity-feed
# Workspaces — /workspaces/me, /workspaces/me/quota
# ---------------------------------------------------------------------------


class CampaignCounts(BaseModel):
    total: int
    live: int
    paused: int
    draft: int
    completed: int


class LeadCounts(BaseModel):
    total: int


class SenderCounts(BaseModel):
    total: int
    sending_today: int
    warmed: int
    throttled: int


class ReplyCounts(BaseModel):
    total: int
    pending_review: int
    urgent: bool


class DomainCounts(BaseModel):
    total: int
    healthy: int
    attention: int


class NotificationCounts(BaseModel):
    unread: int
    critical: int
    warning: int
    urgent: bool


class CountsResponse(BaseModel):
    campaigns: CampaignCounts
    leads: LeadCounts
    senders: SenderCounts
    replies: ReplyCounts
    domains: DomainCounts
    notifications: NotificationCounts


_COUNTS_TTL = 30
_STATUS_BUCKETS = {"active": "live", "launching": "live", "paused": "paused", "draft": "draft", "completed": "completed", "archived": "completed"}


def _bucket_campaigns(campaigns: list) -> CampaignCounts:
    buckets = {"live": 0, "paused": 0, "draft": 0, "completed": 0}
    for c in campaigns:
        status = str(c.get("status") or "").lower()
        bucket = _STATUS_BUCKETS.get(status)
        if bucket:
            buckets[bucket] += 1
    return CampaignCounts(total=len(campaigns), **buckets)


def _leads_total() -> int:
    try:
        resp = emailbison.get_leads_paginated(page=1, per_page=1)
        if isinstance(resp, dict):
            meta = resp.get("meta") or {}
            if "total" in meta:
                return int(meta["total"])
    except Exception as e:
        logger.debug(f"EmailBison leads pagination total failed: {e}")
    try:
        latest = (
            get_supabase()
            .table("lead_engagement_snapshots")
            .select("lead_id", count="exact")
            .eq("snapshot_date", _today())
            .limit(1)
            .execute()
        )
        if latest.count is not None:
            return int(latest.count)
    except Exception as e:
        logger.debug(f"lead_engagement_snapshots count failed: {e}")
    return 0


def _sender_counts() -> SenderCounts:
    supabase = get_supabase()
    try:
        perf_rows = (
            supabase.table("sender_email_performance")
            .select("sender_email_id,warmup_score,in_recovery")
            .eq("snapshot_date", _today())
            .execute()
            .data or []
        )
        if not perf_rows:
            perf_rows = supabase.rpc("get_latest_sender_stats", {}).execute().data or []
    except Exception as e:
        logger.warning(f"sender performance count lookup failed: {e}")
        perf_rows = []

    warmed = sum(1 for r in perf_rows if _compute_warm_state(r) == "warmed")
    throttled = sum(1 for r in perf_rows if r.get("in_recovery"))

    sending_today = 0
    try:
        rows = (
            supabase.table("sender_daily_stats")
            .select("sender_email_id")
            .eq("stat_date", _today())
            .gt("emails_sent", 0)
            .execute()
            .data or []
        )
        sending_today = len({r.get("sender_email_id") for r in rows if r.get("sender_email_id") is not None})
    except Exception as e:
        logger.warning(f"sender_daily_stats sending_today lookup failed: {e}")

    return SenderCounts(
        total=len({r.get("sender_email_id") for r in perf_rows}),
        sending_today=sending_today,
        warmed=warmed,
        throttled=throttled,
    )


def _reply_counts() -> ReplyCounts:
    supabase = get_supabase()
    total = 0
    pending = 0
    try:
        res = supabase.table("reply_events").select("reply_id", count="exact").limit(1).execute()
        total = int(res.count or 0)
    except Exception as e:
        logger.warning(f"reply_events count failed: {e}")
    try:
        res = (
            supabase.table("reply_review_state")
            .select("reply_id", count="exact")
            .eq("review_state", "pending")
            .limit(1)
            .execute()
        )
        pending = int(res.count or 0)
    except Exception as e:
        logger.debug(f"reply_review_state pending count failed: {e}")
    return ReplyCounts(total=total, pending_review=pending, urgent=pending > 0)


def _domain_counts() -> DomainCounts:
    try:
        rows = get_supabase().table("v_domain_health").select("domain,latest_passed").execute().data or []
    except Exception as e:
        logger.warning(f"v_domain_health count failed: {e}")
        rows = []
    healthy = sum(1 for r in rows if r.get("latest_passed"))
    return DomainCounts(total=len(rows), healthy=healthy, attention=len(rows) - healthy)


def _notification_counts() -> NotificationCounts:
    try:
        rows = (
            get_supabase()
            .table("notifications")
            .select("severity")
            .eq("read", False)
            .eq("resolved", False)
            .execute()
            .data or []
        )
    except Exception as e:
        logger.warning(f"notification count failed: {e}")
        rows = []
    critical = sum(1 for r in rows if r.get("severity") == "critical")
    warning = sum(1 for r in rows if r.get("severity") == "warning")
    return NotificationCounts(unread=len(rows), critical=critical, warning=warning, urgent=critical > 0)


def _fetch_campaigns_list() -> list:
    try:
        return emailbison.get_campaigns()
    except Exception as e:
        logger.warning(f"EmailBison campaigns fetch failed for /counts: {e}")
        return []


@app.get("/counts", dependencies=[Security(require_api_key)], response_model=CountsResponse)
def get_counts(workspace_id: Optional[str] = None):
    """
    Aggregated counts powering sidebar badges and hero summaries.
    Cached for 5 seconds. Sub-aggregates are fanned out in parallel so the
    endpoint's latency is bounded by the slowest single query.
    """
    cache_key = f"counts:{workspace_id or 'default'}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    with ThreadPoolExecutor(max_workers=6) as pool:
        campaigns_f = pool.submit(_fetch_campaigns_list)
        leads_f = pool.submit(_leads_total)
        senders_f = pool.submit(_sender_counts)
        replies_f = pool.submit(_reply_counts)
        domains_f = pool.submit(_domain_counts)
        notifs_f = pool.submit(_notification_counts)

        response = CountsResponse(
            campaigns=_bucket_campaigns(campaigns_f.result()),
            leads=LeadCounts(total=leads_f.result()),
            senders=senders_f.result(),
            replies=replies_f.result(),
            domains=domains_f.result(),
            notifications=notifs_f.result(),
        )
    _cache_set(cache_key, response, _COUNTS_TTL)
    return response


class StageCount(BaseModel):
    count: int
    pct: float


class LeadPipelineSummary(BaseModel):
    total: int
    stages: Dict[str, StageCount]
    as_of: str


_FUNNEL_TO_STAGE = {
    "uploaded": "new",
    "contacted": "contacted",
    "opened": "opened",
    "replied": "replied",
    "interested": "booked",
}


@app.get("/lead-pipeline-summary", dependencies=[Security(require_api_key)], response_model=LeadPipelineSummary)
def get_lead_pipeline_summary(workspace_id: Optional[str] = None, campaign_id: Optional[str] = None):
    """
    Mutually-exclusive funnel counts: new / contacted / opened / replied / booked / unsubscribed.
    Each lead is attributed to its highest-reached stage. Percentages computed server-side.
    """
    supabase = get_supabase()
    try:
        query = (
            supabase.table("lead_engagement_snapshots")
            .select("lead_id,funnel_stage,snapshot_date,campaign_engagements")
            .eq("snapshot_date", _today())
        )
        rows = query.execute().data or []
        if not rows:
            latest = (
                supabase.table("lead_engagement_snapshots")
                .select("snapshot_date")
                .order("snapshot_date", desc=True)
                .limit(1)
                .execute()
                .data or []
            )
            if latest:
                rows = (
                    supabase.table("lead_engagement_snapshots")
                    .select("lead_id,funnel_stage,snapshot_date,campaign_engagements")
                    .eq("snapshot_date", latest[0]["snapshot_date"])
                    .execute()
                    .data or []
                )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if campaign_id:
        def _has_campaign(row: dict) -> bool:
            engagements = row.get("campaign_engagements") or []
            for e in engagements:
                if str(e.get("campaign_id")) == str(campaign_id):
                    return True
            return False
        rows = [r for r in rows if _has_campaign(r)]

    stage_counts: dict[str, int] = {"new": 0, "contacted": 0, "opened": 0, "replied": 0, "booked": 0, "unsubscribed": 0}
    for row in rows:
        stage = _FUNNEL_TO_STAGE.get((row.get("funnel_stage") or "").lower())
        if stage:
            stage_counts[stage] += 1

    try:
        unsub_rows = (
            supabase.table("reply_events")
            .select("lead_id", count="exact")
            .eq("folder", "unsubscribed")
            .limit(1)
            .execute()
        )
        stage_counts["unsubscribed"] = int(unsub_rows.count or 0)
    except Exception as e:
        logger.debug(f"unsubscribed count lookup failed: {e}")

    total = sum(stage_counts.values())
    stages = {
        name: StageCount(count=c, pct=round((c / total * 100) if total else 0.0, 1))
        for name, c in stage_counts.items()
    }
    return LeadPipelineSummary(total=total, stages=stages, as_of=datetime.now(timezone.utc).isoformat())


ActivityKind = Literal["reply", "meeting", "milestone", "bounce", "warn", "info"]
ActivitySeverity = Literal["info", "warning", "critical", "success"]


class ActivityEntity(BaseModel):
    type: str
    id: str
    href: Optional[str] = None


class ActivityItem(BaseModel):
    id: str
    kind: ActivityKind
    severity: ActivitySeverity
    title: str
    body: Optional[str] = None
    entity: ActivityEntity
    occurred_at: str


class ActivityFeedResponse(BaseModel):
    items: List[ActivityItem]
    as_of: str


_NOTIF_KIND_MAP = {
    "lead_replied": ("reply", "info"),
    "lead_interested": ("meeting", "success"),
    "account_disconnected": ("warn", "critical"),
    "account_reconnected": ("info", "success"),
    "domain_blacklisted": ("warn", "critical"),
}


def _activity_from_notification(n: dict) -> dict:
    kind, severity = _NOTIF_KIND_MAP.get(n.get("type") or "", ("info", n.get("severity") or "info"))
    severity = n.get("severity") or severity
    if severity == "resolved":
        severity = "success"
    entity_type = n.get("entity_type") or "notification"
    entity_id = str(n.get("entity_id") or n.get("id"))
    return {
        "id": f"n-{n.get('id')}",
        "kind": kind,
        "severity": severity,
        "title": n.get("title") or "",
        "body": n.get("body"),
        "entity": {"type": entity_type, "id": entity_id},
        "occurred_at": n.get("created_at"),
        "_dedup_key": f"{entity_type}:{entity_id}",
    }


def _activity_from_reply(r: dict) -> dict:
    classification = (r.get("classification") or "").lower()
    kind = "meeting" if classification == "interested" else "reply"
    severity = "success" if kind == "meeting" else "info"
    name = r.get("lead_email") or "A lead"
    campaign_name = r.get("campaign_name") or "a campaign"
    reply_id = str(r.get("reply_id"))
    return {
        "id": f"r-{reply_id}",
        "kind": kind,
        "severity": severity,
        "title": f"{name} replied to {campaign_name}",
        "body": r.get("subject"),
        "entity": {"type": "reply", "id": reply_id, "href": f"/replies/{reply_id}"},
        "occurred_at": r.get("replied_at"),
        "_dedup_key": f"lead:{r.get('lead_id')}",
    }


def _activity_from_domain(d: dict) -> dict:
    domain = d.get("domain") or "unknown"
    return {
        "id": f"d-{domain}-{d.get('last_tested_at')}",
        "kind": "warn",
        "severity": "warning",
        "title": f"Deliverability drop on {domain}",
        "body": d.get("latest_status"),
        "entity": {"type": "domain", "id": domain},
        "occurred_at": d.get("last_tested_at"),
        "_dedup_key": f"domain:{domain}",
    }


def _parse_iso(ts: Optional[str]) -> datetime:
    if not ts:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


@app.get("/activity-feed", dependencies=[Security(require_api_key)], response_model=ActivityFeedResponse)
def get_activity_feed(limit: int = 20, workspace_id: Optional[str] = None):
    """
    Unified server-sorted timeline merging notifications + reply_events + domain_health drops.
    Deduplicated on (entity_type, entity_id) within ±5 minutes — notifications win.
    """
    if limit < 1:
        limit = 1
    if limit > 50:
        limit = 50

    supabase = get_supabase()
    pool_size = limit * 3

    try:
        notifications = (
            supabase.table("notifications")
            .select(_NOTIFICATION_COLS)
            .order("created_at", desc=True)
            .limit(pool_size)
            .execute()
            .data or []
        )
    except Exception as e:
        logger.warning(f"activity-feed notifications query failed: {e}")
        notifications = []

    try:
        replies = (
            supabase.table("reply_events")
            .select("reply_id,lead_id,lead_email,campaign_name,subject,classification,replied_at")
            .order("replied_at", desc=True)
            .limit(pool_size)
            .execute()
            .data or []
        )
    except Exception as e:
        logger.warning(f"activity-feed reply_events query failed: {e}")
        replies = []

    try:
        domains = (
            supabase.table("v_domain_health")
            .select("domain,latest_passed,latest_status,last_tested_at")
            .order("last_tested_at", desc=True)
            .limit(pool_size)
            .execute()
            .data or []
        )
    except Exception as e:
        logger.warning(f"activity-feed domain_health query failed: {e}")
        domains = []

    items: list[dict] = []
    items.extend(_activity_from_notification(n) for n in notifications)
    items.extend(_activity_from_reply(r) for r in replies)
    items.extend(_activity_from_domain(d) for d in domains if d.get("latest_passed") is False)
    items = [it for it in items if it.get("occurred_at")]

    items.sort(key=lambda x: _parse_iso(x.get("occurred_at")), reverse=True)

    seen_keys: dict[str, datetime] = {}
    deduped: list[dict] = []
    for item in items:
        key = item["_dedup_key"]
        ts = _parse_iso(item.get("occurred_at"))
        prior = seen_keys.get(key)
        if prior and abs((prior - ts).total_seconds()) <= 300:
            if item["id"].startswith("n-"):
                deduped = [d for d in deduped if d["_dedup_key"] != key or d["id"].startswith("n-")]
            else:
                continue
        seen_keys[key] = ts
        deduped.append(item)
        if len(deduped) >= limit:
            break

    for it in deduped:
        it.pop("_dedup_key", None)
    return ActivityFeedResponse(items=deduped, as_of=datetime.now(timezone.utc).isoformat())


class WorkspaceMe(BaseModel):
    id: str
    name: str
    plan: str
    created_at: Optional[str] = None
    owner_email: Optional[str] = None
    member_count: int


class WorkspaceQuota(BaseModel):
    period_start: str
    period_end: str
    used: int
    limit: Optional[int] = None
    pct_used: Optional[float] = None
    plan_tier: str
    upgrade_url: str


@app.get("/workspaces/me", dependencies=[Security(require_api_key)], response_model=WorkspaceMe)
def get_workspace_me():
    """Singleton workspace record derived from env vars (single-tenant deployment)."""
    created_at = os.environ.get("WORKSPACE_CREATED_AT")
    if not created_at:
        try:
            earliest = (
                get_supabase()
                .table("workspace_daily_stats")
                .select("stat_date")
                .order("stat_date")
                .limit(1)
                .execute()
                .data or []
            )
            if earliest:
                created_at = f"{earliest[0]['stat_date']}T00:00:00Z"
        except Exception:
            pass
    members_env = os.environ.get("WORKSPACE_MEMBERS", "")
    member_count = len([m for m in members_env.split(",") if m.strip()]) or 1
    return WorkspaceMe(
        id=os.environ.get("WORKSPACE_ID", "ws_default"),
        name=os.environ.get("WORKSPACE_NAME", "Default Workspace"),
        plan=os.environ.get("WORKSPACE_PLAN", "growth"),
        created_at=created_at,
        owner_email=os.environ.get("WORKSPACE_OWNER_EMAIL"),
        member_count=member_count,
    )


@app.get("/workspaces/me/quota", dependencies=[Security(require_api_key)], response_model=WorkspaceQuota)
def get_workspace_quota():
    """Monthly send quota for the current calendar month, computed from workspace_daily_stats."""
    now = datetime.now(timezone.utc)
    period_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    if now.month == 12:
        next_month = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        next_month = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
    period_end = next_month - timedelta(seconds=1)

    used = 0
    try:
        rows = (
            get_supabase()
            .table("workspace_daily_stats")
            .select("emails_sent")
            .gte("stat_date", period_start.date().isoformat())
            .execute()
            .data or []
        )
        used = sum(int(r.get("emails_sent") or 0) for r in rows)
    except Exception as e:
        logger.warning(f"workspace quota used lookup failed: {e}")

    limit_env = os.environ.get("WORKSPACE_SEND_LIMIT")
    limit = int(limit_env) if limit_env and limit_env.isdigit() else None
    pct = round(used / limit * 100, 2) if limit else None

    return WorkspaceQuota(
        period_start=period_start.isoformat(),
        period_end=period_end.isoformat(),
        used=used,
        limit=limit,
        pct_used=pct,
        plan_tier=os.environ.get("WORKSPACE_PLAN", "growth"),
        upgrade_url="/settings/billing",
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

def _upsert_review_state(reply_id: str, fields: dict) -> None:
    row = {"reply_id": reply_id, "updated_at": datetime.now(timezone.utc).isoformat(), **fields}
    try:
        get_supabase().table("reply_review_state").upsert(row, on_conflict="reply_id").execute()
    except Exception as e:
        logger.error(f"Failed to upsert reply_review_state for {reply_id}: {e}")


@app.post("/actions/replies/{reply_id}/mark-interested", dependencies=[Security(require_api_key)])
async def mark_interested(reply_id: str, x_user_email: str = Header(default=None)):
    try:
        result = emailbison.patch(f"/api/replies/{reply_id}/interested")
        _upsert_review_state(reply_id, {"review_state": "classified", "classification": "interested"})
        log_action("reply_mark_interested", "reply", reply_id, api_response=result, performed_by=x_user_email)
        return {"success": True}
    except Exception as e:
        log_action("reply_mark_interested", "reply", reply_id, status="error", error_message=str(e), performed_by=x_user_email)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/actions/replies/{reply_id}/mark-not-interested", dependencies=[Security(require_api_key)])
async def mark_not_interested(reply_id: str, x_user_email: str = Header(default=None)):
    try:
        result = emailbison.patch(f"/api/replies/{reply_id}/uninterested")
        _upsert_review_state(reply_id, {"review_state": "classified", "classification": "not_interested"})
        log_action("reply_mark_not_interested", "reply", reply_id, api_response=result, performed_by=x_user_email)
        return {"success": True}
    except Exception as e:
        log_action("reply_mark_not_interested", "reply", reply_id, status="error", error_message=str(e), performed_by=x_user_email)
        raise HTTPException(status_code=500, detail=str(e))


class ReviewStateRequest(BaseModel):
    state: ReviewState


@app.patch("/actions/replies/{reply_id}/read", dependencies=[Security(require_api_key)])
async def mark_reply_read(reply_id: str, x_user_email: str = Header(default=None)):
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
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/actions/replies/{reply_id}/review-state", dependencies=[Security(require_api_key)])
async def update_review_state(reply_id: str, body: ReviewStateRequest, x_user_email: str = Header(default=None)):
    try:
        _upsert_review_state(reply_id, {"review_state": body.state})
        log_action("reply_update_review_state", "reply", reply_id, payload=body.dict(), performed_by=x_user_email)
        return {"success": True}
    except Exception as e:
        log_action("reply_update_review_state", "reply", reply_id, status="error", error_message=str(e), performed_by=x_user_email)
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
