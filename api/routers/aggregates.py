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
from lib.supabase_paginate import fetch_all
from lib.warmup_report import get_warmup_report
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


def _target_workspaces(ws: Optional[str]) -> list:
    """Bison workspaces to aggregate over: one when filtered, else all pollable."""
    pollable = config.pollable_workspaces()
    if ws:
        return [w for w in pollable if w["id"] == ws]
    return pollable


def _leads_total(ws: Optional[str]) -> int:
    total = 0
    got_live = False
    for w in _target_workspaces(ws):
        try:
            resp = emailbison.for_workspace(w["id"]).get_leads_paginated(page=1, per_page=1)
            if isinstance(resp, dict):
                meta = resp.get("meta") or {}
                if "total" in meta:
                    total += int(meta["total"])
                    got_live = True
        except Exception as e:
            logger.debug(f"EmailBison leads total failed for {w['id']}: {e}")
    if got_live:
        return total
    try:
        q = (
            get_supabase()
            .table("lead_engagement_snapshots")
            .select("lead_id", count="exact")
            .eq("snapshot_date", _today())
        )
        if ws:
            q = q.eq("workspace_id", ws)
        latest = q.limit(1).execute()
        if latest.count is not None:
            return int(latest.count)
    except Exception as e:
        logger.debug(f"lead_engagement_snapshots count failed: {e}")
    return 0


def _sender_counts(ws: Optional[str]) -> SenderCounts:
    # Bison sender IDs are per-workspace integers and collide across
    # workspaces, so all dedup keys must include workspace_id.
    supabase = get_supabase()

    # Per-workspace: today's snapshot when present, else that workspace's
    # latest snapshot (matches what the Senders page shows). A workspace with
    # no data today (e.g. V1 after its senders moved to V2) would otherwise
    # vanish from the "all" total.
    ws_ids = [w["id"] for w in _target_workspaces(ws)] or ([ws] if ws else [])
    perf_rows: list = []
    for wid in ws_ids:
        try:
            rows = fetch_all(
                lambda wid=wid: supabase.table("sender_email_performance")
                .select("workspace_id,sender_email_id,warmup_score,in_recovery")
                .eq("snapshot_date", _today())
                .eq("workspace_id", wid)
                .order("sender_email_id")
            )
            if not rows:
                rows = (
                    supabase.rpc("get_latest_sender_stats", {"p_workspace_id": wid})
                    .execute().data or []
                )
            perf_rows.extend(rows)
        except Exception as e:
            logger.warning(f"sender performance count lookup failed for {wid}: {e}")

    warmed = sum(1 for r in perf_rows if _compute_warm_state(r) == "warmed")
    throttled = sum(1 for r in perf_rows if r.get("in_recovery"))

    def _sending_query():
        q = (
            supabase.table("sender_daily_stats")
            .select("workspace_id,sender_email_id")
            .eq("stat_date", _today())
            .gt("emails_sent", 0)
        )
        if ws:
            q = q.eq("workspace_id", ws)
        return q.order("workspace_id").order("sender_email_id")

    sending_today = 0
    try:
        rows = fetch_all(_sending_query)
        sending_today = len({
            (r.get("workspace_id"), r.get("sender_email_id"))
            for r in rows if r.get("sender_email_id") is not None
        })
    except Exception as e:
        logger.warning(f"sender_daily_stats sending_today lookup failed: {e}")

    return SenderCounts(
        total=len({(r.get("workspace_id"), r.get("sender_email_id")) for r in perf_rows}),
        sending_today=sending_today,
        warmed=warmed,
        throttled=throttled,
    )


def _reply_counts(ws: Optional[str]) -> ReplyCounts:
    supabase = get_supabase()
    total = 0
    pending = 0
    try:
        q = supabase.table("reply_events").select("reply_id", count="exact")
        if ws:
            q = q.eq("workspace_id", ws)
        res = q.limit(1).execute()
        total = int(res.count or 0)
    except Exception as e:
        logger.warning(f"reply_events count failed: {e}")
    try:
        # reply_review_state has no workspace column — pending stays global.
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


def _fetch_campaigns_list(ws: Optional[str]) -> list:
    out: list = []
    for w in _target_workspaces(ws):
        try:
            out.extend(emailbison.for_workspace(w["id"]).get_campaigns())
        except Exception as e:
            logger.warning(f"EmailBison campaigns fetch failed for /counts ({w['id']}): {e}")
    return out


@router.get("/counts", dependencies=[Security(require_api_key)], response_model=CountsResponse)
def get_counts(workspace_id: Optional[str] = None):
    """
    Aggregated counts powering sidebar badges and hero summaries, scoped to
    the selected workspace ("all"/absent = every pollable workspace).
    Cached briefly. Sub-aggregates are fanned out in parallel so the
    endpoint's latency is bounded by the slowest single query.
    Domains and notifications have no workspace dimension — always global.
    """
    ws = workspace_id if workspace_id and workspace_id != "all" else None
    cache_key = f"counts:{ws or 'all'}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    with ThreadPoolExecutor(max_workers=6) as pool:
        campaigns_f = pool.submit(_fetch_campaigns_list, ws)
        leads_f = pool.submit(_leads_total, ws)
        senders_f = pool.submit(_sender_counts, ws)
        replies_f = pool.submit(_reply_counts, ws)
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


@router.get("/lead-pipeline-summary", dependencies=[Security(require_api_key)], response_model=LeadPipelineSummary)
def get_lead_pipeline_summary(workspace_id: Optional[str] = None, campaign_id: Optional[str] = None):
    """
    Mutually-exclusive funnel counts: new / contacted / opened / replied / booked / unsubscribed.
    Each lead is attributed to its highest-reached stage. Percentages computed server-side.
    """
    supabase = get_supabase()
    ws = workspace_id if workspace_id and workspace_id != "all" else None

    def _snapshot_query(snapshot_date: str):
        q = (
            supabase.table("lead_engagement_snapshots")
            .select("lead_id,funnel_stage,snapshot_date,campaign_engagements")
            .eq("snapshot_date", snapshot_date)
        )
        return q.eq("workspace_id", ws) if ws else q

    try:
        rows = _snapshot_query(_today()).execute().data or []
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
                rows = _snapshot_query(latest[0]["snapshot_date"]).execute().data or []
    except Exception:
        raise

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
        unsub_q = (
            supabase.table("reply_events")
            .select("lead_id", count="exact")
            .eq("folder", "unsubscribed")
        )
        if ws:
            unsub_q = unsub_q.eq("workspace_id", ws)
        unsub_rows = unsub_q.limit(1).execute()
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


@router.get("/activity-feed", dependencies=[Security(require_api_key)], response_model=ActivityFeedResponse)
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
    ws = workspace_id if workspace_id and workspace_id != "all" else None
    pool_size = limit * 3

    try:
        # Notifications have no workspace column — always global.
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
        replies_q = (
            supabase.table("reply_events")
            .select("reply_id,lead_id,lead_email,campaign_name,subject,classification,replied_at")
            .order("replied_at", desc=True)
        )
        if ws:
            replies_q = replies_q.eq("workspace_id", ws)
        replies = replies_q.limit(pool_size).execute().data or []
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


@router.get("/warmup/report", dependencies=[Security(require_api_key)])
def warmup_report(
    workspace_id: Optional[str] = None,
    date: Optional[str] = None,
):
    """Slack-style warmup fleet report.

    Buckets: not warming, ≥95, 90–94, <90. Optional workspace_id (omit/all =
    V1+V2 aggregate). Today is computed live from sender_email_performance;
    historical dates serve warmup_daily_report snapshots.
    """
    if date:
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")
    return get_warmup_report(workspace_id=workspace_id, date=date)


@router.get("/workspaces/me", dependencies=[Security(require_api_key)], response_model=WorkspaceMe)
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


@router.get("/workspaces/me/quota", dependencies=[Security(require_api_key)], response_model=WorkspaceQuota)
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


