"""Daily Review — today's planned sending schedule from EmailBison.

GET /schedule/today aggregates the scheduled (not yet sent) emails for every
active campaign into planned counts per campaign and per inbox, filtered to
the current UTC day. Read-only: never mutates anything on the Bison side.

Results are cached in-process for ~10 minutes; the manual data refresh
(POST /actions/refresh) clears the cache so the next request re-fetches.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Security

from lib import config, emailbison
from api.deps import require_api_key, _cache_get_stale, _cache_set, _cache_revalidate

logger = logging.getLogger(__name__)
router = APIRouter()

_SCHEDULE_CACHE_TTL = 600  # seconds
_MAX_PAGES = 30            # safety cap per campaign
_ACTIVE_STATUSES = {"active", "running"}


def _fetch_scheduled_emails(client, campaign_id: str) -> List[dict]:
    """All scheduled emails for a campaign, following Laravel-style pagination."""
    rows: List[dict] = []
    page = 1
    while page <= _MAX_PAGES:
        res = client.get(
            f"/api/campaigns/{campaign_id}/scheduled-emails",
            params={"page": page},
        )
        if isinstance(res, list):
            rows.extend(res)
            break
        if not isinstance(res, dict):
            break
        batch = res.get("data") or []
        if not isinstance(batch, list):
            break
        rows.extend(batch)
        meta = res.get("meta") or {}
        last_page = meta.get("last_page")
        current = meta.get("current_page", page)
        if not last_page or int(current) >= int(last_page):
            break
        page = int(current) + 1
    return rows


def _scheduled_day_utc(item: dict) -> Optional[str]:
    """UTC calendar date ("YYYY-MM-DD") an item is scheduled for, if parseable."""
    raw = None
    for field in ("scheduled_at", "send_at", "send_time", "scheduled_datetime",
                  "scheduled_date", "scheduled_for", "created_at"):
        raw = item.get(field)
        if raw:
            break
    if not raw:
        return None
    text = str(raw).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).date().isoformat()
    except ValueError:
        # Fall back to a bare leading date ("YYYY-MM-DD ...").
        return text[:10] if len(text) >= 10 and text[4:5] == "-" else None


def _inbox_email(item: dict) -> str:
    """Sender inbox address for a scheduled email, tolerant of payload shapes."""
    for field in ("sender_email", "email_account", "sender", "from"):
        val = item.get(field)
        if isinstance(val, dict):
            email = val.get("email") or val.get("email_address")
            if email:
                return str(email)
        elif isinstance(val, str) and "@" in val:
            return val
    for field in ("from_email", "sender_email_address"):
        val = item.get(field)
        if isinstance(val, str) and "@" in val:
            return val
    return "unknown"


def _plan_for_workspace(ws: dict, today: str) -> List[Dict[str, Any]]:
    """Planned sends today per active campaign (with per-inbox breakdown)."""
    client = emailbison.for_workspace(ws["id"])
    campaigns = client.get_campaigns()
    active = [
        c for c in campaigns
        if str(c.get("status") or "").lower() in _ACTIVE_STATUSES
    ]

    def one(c: dict) -> Dict[str, Any]:
        cid = str(c.get("id"))
        entry: Dict[str, Any] = {
            "workspace_id": ws["id"],
            "workspace_name": ws["name"],
            "campaign_id": cid,
            "campaign_name": c.get("name", ""),
            "planned_today": 0,
            "inboxes": [],
            "error": None,
        }
        try:
            items = _fetch_scheduled_emails(client, cid)
        except Exception as e:
            logger.warning(f"scheduled-emails fetch failed for campaign {cid}: {e}")
            entry["error"] = "fetch_failed"
            return entry
        per_inbox: Dict[str, int] = {}
        for item in items:
            if _scheduled_day_utc(item) != today:
                continue
            entry["planned_today"] += 1
            inbox = _inbox_email(item)
            per_inbox[inbox] = per_inbox.get(inbox, 0) + 1
        entry["inboxes"] = [
            {"email": email, "planned": count}
            for email, count in sorted(per_inbox.items(), key=lambda kv: -kv[1])
        ]
        return entry

    if not active:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(active))) as pool:
        return list(pool.map(one, active))


@router.get("/schedule/today", dependencies=[Security(require_api_key)])
def schedule_today(workspace_id: Optional[str] = None):
    """Today's planned sending volume (UTC day) per campaign and per inbox.

    Aggregated from EmailBison scheduled emails for active campaigns.
    Cached ~10 minutes; the manual data refresh busts the cache.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    ws_filter = workspace_id if workspace_id and workspace_id != "all" else None
    cache_key = f"schedule_today:{ws_filter or 'all'}:{today}"

    def build() -> Dict[str, Any]:
        # Only token-configured workspaces can be queried on the Bison side.
        pollable = config.pollable_workspaces()
        workspaces = (
            [w for w in pollable if w["id"] == ws_filter]
            if ws_filter else pollable
        )
        campaigns: List[Dict[str, Any]] = []
        for ws in workspaces:
            try:
                campaigns.extend(_plan_for_workspace(ws, today))
            except Exception as e:
                logger.warning(f"schedule fetch failed for workspace {ws['id']}: {e}")

        campaigns.sort(key=lambda c: -c["planned_today"])
        return {
            "date": today,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "planned_total": sum(c["planned_today"] for c in campaigns),
            "campaigns": campaigns,
        }

    cached, fresh = _cache_get_stale(cache_key)
    if fresh:
        return cached
    if cached is not None:
        # Building takes ~20s of Bison paging; serve the stale payload now and
        # rebuild in the background. Manual refresh clears the cache entirely,
        # so that path still blocks for genuinely fresh data.
        _cache_revalidate(cache_key, build, _SCHEDULE_CACHE_TTL)
        return cached

    payload = build()
    _cache_set(cache_key, payload, _SCHEDULE_CACHE_TTL)
    return payload
