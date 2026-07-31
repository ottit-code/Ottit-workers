"""Shared helpers for reading EmailBison's scheduled-emails queue.

Used by the /schedule/today endpoint (live "remaining" view) and by
send_plan_snapshotter (midnight capture of the full day's plan).
Read-only against Bison.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from lib import emailbison

logger = logging.getLogger(__name__)

# Safety cap per campaign. Bison pages at 15/page and queues can hold weeks
# of backlog (observed: 16k+ items / 1,100 pages on one campaign), so the cap
# must be generous or today's counts silently clip.
MAX_PAGES = 2000
ACTIVE_STATUSES = {"active", "running"}
_PAGE_WORKERS = 10


def _page_days(rows: list) -> List[str]:
    days: List[str] = []
    for item in rows:
        d = scheduled_day_utc(item)
        if d:
            days.append(d)
    return days


def fetch_scheduled_emails(
    client, campaign_id: str, through_day: Optional[str] = None
) -> List[dict]:
    """Scheduled emails for a campaign, following Laravel-style pagination.

    Pages are fetched concurrently (Bison ignores per_page and always returns
    15/page). When `through_day` is set and the queue is ascending by
    scheduled_date (Bison's default), paging stops after the last page that
    can still contain that day — weeks of future backlog are skipped.
    """
    first = client.get(
        f"/api/campaigns/{campaign_id}/scheduled-emails", params={"page": 1}
    )
    if isinstance(first, list):
        return first
    if not isinstance(first, dict):
        return []
    rows: List[dict] = list(first.get("data") or [])
    meta = first.get("meta") or {}
    last_page = min(int(meta.get("last_page") or 1), MAX_PAGES)
    if last_page <= 1:
        return rows

    def one_page(page: int) -> list:
        try:
            res = client.get(
                f"/api/campaigns/{campaign_id}/scheduled-emails",
                params={"page": page},
            )
            return res.get("data") or [] if isinstance(res, dict) else []
        except Exception as e:
            logger.warning(
                f"scheduled-emails page {page} failed for campaign {campaign_id}: {e}"
            )
            return []

    end_page = last_page
    already_have_last = False
    if through_day and last_page > 1:
        # Probe the last page. If everything there is after through_day and
        # page 1 still has through_day-or-earlier items, binary-search the
        # cutover so we don't download the entire future backlog.
        last_rows = one_page(last_page)
        first_days = _page_days(rows)
        last_days = _page_days(last_rows)
        if (
            first_days
            and last_days
            and min(first_days) <= through_day < min(last_days)
        ):
            lo, hi = 1, last_page
            while lo < hi:
                mid = (lo + hi + 1) // 2
                mid_rows = one_page(mid) if mid != last_page else last_rows
                mid_days = _page_days(mid_rows)
                if mid_days and min(mid_days) > through_day:
                    hi = mid - 1
                else:
                    lo = mid
            end_page = max(lo, 1)
        if end_page >= last_page:
            rows.extend(last_rows)
            already_have_last = True

    if end_page <= 1:
        return rows

    pages = list(range(2, end_page + 1))
    if already_have_last:
        pages = [p for p in pages if p != last_page]
    if not pages:
        return rows

    with ThreadPoolExecutor(max_workers=_PAGE_WORKERS) as pool:
        for batch in pool.map(one_page, pages):
            rows.extend(batch)
    return rows


def scheduled_dt_utc(item: dict) -> Optional[datetime]:
    """UTC datetime an item is scheduled for, if parseable.

    Bison's canonical field is scheduled_date (UTC, Z-suffixed); the others
    are fallbacks for older payload shapes.
    """
    raw = None
    for field in ("scheduled_date", "scheduled_at", "send_at", "send_time",
                  "scheduled_datetime", "scheduled_for"):
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
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def scheduled_day_utc(item: dict) -> Optional[str]:
    """UTC calendar date ("YYYY-MM-DD") an item is scheduled for, if parseable."""
    dt = scheduled_dt_utc(item)
    if dt is not None:
        return dt.date().isoformat()
    # Fall back to a bare leading date ("YYYY-MM-DD ...").
    for field in ("scheduled_date", "scheduled_at", "send_at"):
        text = str(item.get(field) or "").strip()
        if len(text) >= 10 and text[4:5] == "-":
            return text[:10]
    return None


def inbox_email(item: dict) -> str:
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


def plan_for_workspace(ws: dict, day: str) -> List[Dict[str, Any]]:
    """Queued sends for the given UTC day, per active campaign (with per-inbox
    breakdown).

    Bison's queue keeps items whose scheduled time has already passed without
    sending (daily limits etc.) — those roll over to later days, so they are
    NOT counted as remaining. planned_today = still-future items only;
    overdue_today = today's items whose slot passed unsent. At midnight
    (snapshotter) everything is future, so planned_today == the full plan.
    """
    client = emailbison.for_workspace(ws["id"])
    now = datetime.now(timezone.utc)
    campaigns = client.get_campaigns()
    active = [
        c for c in campaigns
        if str(c.get("status") or "").lower() in ACTIVE_STATUSES
    ]

    def one(c: dict) -> Dict[str, Any]:
        cid = str(c.get("id"))
        entry: Dict[str, Any] = {
            "workspace_id": ws["id"],
            "workspace_name": ws["name"],
            "campaign_id": cid,
            "campaign_name": c.get("name", ""),
            "planned_today": 0,
            "overdue_today": 0,
            "inboxes": [],
            "error": None,
        }
        try:
            # Only page through the target day — skip weeks of future backlog.
            items = fetch_scheduled_emails(client, cid, through_day=day)
        except Exception as e:
            logger.warning(f"scheduled-emails fetch failed for campaign {cid}: {e}")
            entry["error"] = "fetch_failed"
            return entry
        per_inbox: Dict[str, int] = {}
        for item in items:
            dt = scheduled_dt_utc(item)
            if dt is None or dt.date().isoformat() != day:
                continue
            if dt <= now:
                entry["overdue_today"] += 1
                continue
            entry["planned_today"] += 1
            inbox = inbox_email(item)
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


def campaign_sent_on_day(client, campaign_id: str, day: str) -> Optional[int]:
    """Emails sent by one campaign on the given UTC day, from its line-area
    chart stats (one cheap call — no queue paging). None if the lookup fails."""
    try:
        raw = client.get_campaign_line_area_chart_stats(campaign_id, day, day)
        series = raw.get("data", []) if isinstance(raw, dict) else raw
        for item in series:
            if item.get("label") == "Sent":
                for date_str, count in item.get("dates", []):
                    if date_str == day:
                        return int(count or 0)
                return 0
        return 0
    except Exception as e:
        logger.warning(f"sent-today lookup failed for campaign {campaign_id}: {e}")
        return None


def plan_from_snapshot(ws: dict, day: str, snapshot_rows: List[dict]) -> List[Dict[str, Any]]:
    """Approximate schedule view derived from the midnight plan snapshot.

    remaining ≈ planned - sent_today per campaign. This OVERCOUNTS when Bison
    still holds overdue items (slot passed, will roll to later days) — prefer
    plan_for_workspace for exact remaining. Kept as a fallback when live queue
    paging fails. Inbox breakdowns show the *planned* midnight split.
    """
    client = emailbison.for_workspace(ws["id"])

    def one(row: dict) -> Dict[str, Any]:
        cid = str(row["campaign_id"])
        planned = int(row.get("planned") or 0)
        sent = campaign_sent_on_day(client, cid, day)
        remaining = max(planned - sent, 0) if sent is not None else planned
        return {
            "workspace_id": ws["id"],
            "workspace_name": ws["name"],
            "campaign_id": cid,
            "campaign_name": row.get("campaign_name") or "",
            "planned_today": remaining,
            "planned_start": planned,
            "sent_today": sent,
            "overdue_today": None,  # unknown without queue paging
            "inboxes": row.get("inboxes") or [],
            "error": None if sent is not None else "sent_lookup_failed",
        }

    if not snapshot_rows:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(snapshot_rows))) as pool:
        return list(pool.map(one, snapshot_rows))


def sent_today_for_workspace(ws_id: str, day: str) -> Optional[int]:
    """Emails actually sent so far on the given UTC day, live from Bison's
    workspace chart stats. None if the lookup fails."""
    try:
        client = emailbison.for_workspace(ws_id)
        stats = client.get_workspace_chart_stats(day, day)
        for item in stats.get("data", []):
            if item.get("label") == "Sent":
                for date_str, count in item.get("dates", []):
                    if date_str == day:
                        return int(count or 0)
                return 0
        return 0
    except Exception as e:
        logger.warning(f"sent-today lookup failed for workspace {ws_id}: {e}")
        return None
