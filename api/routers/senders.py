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
from api.logging_utils import log_action
from lib.notifications import create_notification
from api.deps import (  # noqa: F401
    require_api_key,
    _today,
    _bearer,
    _cache_get,
    _cache_get_stale,
    _cache_revalidate,
    _cache_set,
    _cohort_reply_map,
    _merge_cohort_fields,
    _compute_warm_state,
    WarmState,
    _NOTIFICATION_COLS,
    ReviewState,
    Classification,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# Senders — from Supabase
# ---------------------------------------------------------------------------

def _resolve_date_range(
    days: int,
    start_date: Optional[str],
    end_date: Optional[str],
) -> tuple[str, str]:
    """Resolve (start, end) ISO dates from either `days` or explicit bounds.

    Explicit dates take precedence over `days`.
    """
    today = datetime.now(timezone.utc).date()
    try:
        range_end = datetime.fromisoformat(end_date).date() if end_date else today
        range_start = (
            datetime.fromisoformat(start_date).date()
            if start_date
            else range_end - timedelta(days=days)
        )
    except ValueError:
        raise HTTPException(status_code=422, detail="Dates must be YYYY-MM-DD")
    if range_start > range_end:
        raise HTTPException(status_code=422, detail="start_date must be <= end_date")
    return range_start.isoformat(), range_end.isoformat()


def _ws(workspace_id: Optional[str]) -> Optional[str]:
    """Normalise workspace_id: None/'all' means 'no filter'."""
    if workspace_id and workspace_id != "all":
        return workspace_id
    return None


def _strip_internal_tags(raw) -> list:
    """Bison mirrors each bundle tag with an internal "p."-prefixed copy
    (p.CI-DED-Set4-0518, …). Exclude any tag containing "p." as a rule.

    Tags arrive either as plain strings or {"name": ...} dicts.
    """
    out: list = []
    for item in raw or []:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and "p." in name:
            continue
        out.append(item)
    return out


def _latest_perf_map() -> dict[int, dict]:
    """Latest tags + warmup score per sender from sender_email_performance.

    Scans the last 14 days of snapshots (paged past the 1000-row cap) so every
    sender's latest row is seen even with 400+ senders per day.
    """
    since = (datetime.now(timezone.utc).date() - timedelta(days=14)).isoformat()
    try:
        rows = fetch_all(
            lambda: get_supabase()
            .table("sender_email_performance")
            .select("sender_email_id,tags,warmup_score,snapshot_date")
            .gte("snapshot_date", since)
            .order("snapshot_date", desc=True)
            .order("sender_email_id")
        )
    except Exception as e:
        logger.warning(f"latest perf lookup failed: {e}")
        return {}
    out: dict[int, dict] = {}
    for r in rows:
        sid = r.get("sender_email_id")
        if sid is None or int(sid) in out:
            continue
        out[int(sid)] = {
            "tags": _strip_internal_tags(r.get("tags")),
            "warmup_score": r.get("warmup_score"),
        }
    return out


_SENDER_STATS_COLS = (
    "workspace_id,sender_email_id,sender_email,domain,stat_date,"
    "emails_sent,emails_opened,emails_replied,emails_bounced,"
    "warmup_sent,warmup_replied,daily_limit,warmup_enabled,fetched_at"
)

# Bison counters on sender_daily_stats are lifetime cumulative snapshots, so a
# date-range figure is the delta between the last snapshot inside the range and
# the last snapshot before it.
_DELTA_FIELDS = (
    "emails_sent",
    "emails_opened",
    "emails_replied",
    "emails_bounced",
    "warmup_sent",
    "warmup_replied",
)


def _fetch_all_sender_stats(
    range_end: str,
    domain: Optional[str],
    warmup_enabled: Optional[bool],
    ws_filter: Optional[str],
) -> list[dict]:
    """All sender_daily_stats snapshots up to range_end, paged past the
    PostgREST 1000-row cap."""
    def build():
        query = (
            get_supabase()
            .table("sender_daily_stats")
            .select(_SENDER_STATS_COLS)
            .lte("stat_date", range_end)
            .order("sender_email_id")
            .order("stat_date")
        )
        if domain:
            query = query.eq("domain", domain)
        if warmup_enabled is not None:
            query = query.eq("warmup_enabled", warmup_enabled)
        if ws_filter:
            query = query.eq("workspace_id", ws_filter)
        return query

    return fetch_all(build)


def _senders_for_range(
    range_start: str,
    range_end: str,
    domain: Optional[str],
    warmup_enabled: Optional[bool],
    ws_filter: Optional[str],
) -> list[dict]:
    """One row per sender with counters scoped to [range_start, range_end].

    Snapshot metadata (email, domain, daily_limit, warmup_enabled) comes from
    the last snapshot inside the range; a sender with no snapshot in the range
    is excluded (it didn't exist / wasn't tracked yet).
    """
    rows = _fetch_all_sender_stats(range_end, domain, warmup_enabled, ws_filter)

    by_sender: dict[tuple, list[dict]] = {}
    for r in rows:
        key = (r.get("workspace_id"), r.get("sender_email_id"))
        by_sender.setdefault(key, []).append(r)

    out: list[dict] = []
    for history in by_sender.values():
        # history is stat_date-ascending from the query ordering
        end_row = None
        base_row = None
        for r in history:
            if r["stat_date"] < range_start:
                base_row = r
            if range_start <= r["stat_date"] <= range_end:
                end_row = r
        if end_row is None:
            continue
        result = dict(end_row)
        for field in _DELTA_FIELDS:
            base = (base_row or {}).get(field) or 0
            result[field] = max(0, (end_row.get(field) or 0) - base)
        out.append(result)

    out.sort(key=lambda r: r.get("sender_email") or "")
    return out


@router.get("/senders", dependencies=[Security(require_api_key)])
def list_senders(
    domain: Optional[str] = None,
    warmup_enabled: Optional[bool] = None,
    workspace_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    """
    Latest stats per sender from Supabase.
    Falls back to most recent available date via get_latest_sender_stats RPC
    if today has no data yet. Each row includes the sender's latest Bison tags
    and latest warmup_score snapshot.

    Pass start_date/end_date (YYYY-MM-DD, inclusive, UTC) to scope the counters
    to that range instead of lifetime totals. Counters are computed as snapshot
    deltas since Bison reports cumulative totals.

    Responses are cached ~2 minutes (stale-served while revalidating) since
    each build pages through two weeks of perf snapshots. The manual data
    refresh clears the cache.
    """
    cache_key = (
        f"senders:{domain or ''}:{warmup_enabled}:{_ws(workspace_id) or 'all'}"
        f":{start_date or ''}:{end_date or ''}"
    )

    def build():
        return _build_senders(domain, warmup_enabled, workspace_id, start_date, end_date)

    cached, fresh = _cache_get_stale(cache_key)
    if fresh:
        return cached
    if cached is not None:
        _cache_revalidate(cache_key, build, _SENDERS_CACHE_TTL)
        return cached
    rows = build()
    _cache_set(cache_key, rows, _SENDERS_CACHE_TTL)
    return rows


_SENDERS_CACHE_TTL = 120  # seconds


def _build_senders(
    domain: Optional[str],
    warmup_enabled: Optional[bool],
    workspace_id: Optional[str],
    start_date: Optional[str],
    end_date: Optional[str],
):
    supabase = get_supabase()
    ws_filter = _ws(workspace_id)
    if start_date or end_date:
        range_start, range_end = _resolve_date_range(30, start_date, end_date)
        rows = _senders_for_range(range_start, range_end, domain, warmup_enabled, ws_filter)
        # Range replies use cohort attribution (replies to emails SENT in the
        # range, whenever they landed) — the dashboard's agreed definition.
        # Snapshot deltas can't provide this: historical emails_replied
        # snapshots were 0 until the 2026-07-29 poller fix, and deltas count
        # replies by arrival date anyway.
        try:
            cohort = _cohort_reply_map(range_start, range_end, "sender", ws_filter)
            per_sender: dict[str, int] = {}
            for (email, _d), n in cohort.items():
                per_sender[email] = per_sender.get(email, 0) + n
            for row in rows:
                email = (row.get("sender_email") or "").lower()
                row["emails_replied"] = per_sender.get(email, 0)
        except Exception as e:
            logger.warning(f"cohort reply merge failed for /senders range: {e}")
        perf_map = _latest_perf_map()
        for row in rows:
            sid = row.get("sender_email_id")
            perf = perf_map.get(int(sid), {}) if sid is not None else {}
            row["tags"] = perf.get("tags", [])
            row["warmup_score"] = perf.get("warmup_score")
        return rows
    try:
        def build_today():
            query = supabase.table("sender_daily_stats").select("*").eq("stat_date", _today())
            if domain:
                query = query.eq("domain", domain)
            if warmup_enabled is not None:
                query = query.eq("warmup_enabled", warmup_enabled)
            if ws_filter:
                query = query.eq("workspace_id", ws_filter)
            return query.order("sender_email")

        rows = fetch_all(build_today)

        if not rows:
            # Fall back: most recent record per sender via DISTINCT ON RPC
            params: dict = {}
            if domain:
                params["p_domain"] = domain
            if warmup_enabled is not None:
                params["p_warmup_enabled"] = warmup_enabled
            if ws_filter:
                params["p_workspace_id"] = ws_filter
            rows = fetch_all(lambda: supabase.rpc("get_latest_sender_stats", params))

        perf_map = _latest_perf_map()
        for row in rows or []:
            sid = row.get("sender_email_id")
            perf = perf_map.get(int(sid), {}) if sid is not None else {}
            row["tags"] = perf.get("tags", [])
            row["warmup_score"] = perf.get("warmup_score")

        return rows
    except Exception:
        raise


@router.get("/senders/tld-performance", dependencies=[Security(require_api_key)])
def tld_performance(
    days: int = 30,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    workspace_id: Optional[str] = None,
):
    """Per-TLD, per-day aggregates from sender_daily_stats.

    TLD is derived from the last dot-segment of the sender domain (.com, .co, …).
    Rows: {tld, stat_date, senders, emails_sent, emails_replied, emails_bounced,
    warmup_sent, daily_limit}.
    """
    range_start, range_end = _resolve_date_range(days, start_date, end_date)
    params: dict = {"p_start": range_start, "p_end": range_end}
    if _ws(workspace_id):
        params["p_workspace_id"] = _ws(workspace_id)
    try:
        # Paged: a year of per-TLD daily rows exceeds the 1000-row cap.
        rows = fetch_all(lambda: get_supabase().rpc("get_tld_daily_performance", params))
    except Exception:
        raise
    try:
        cohort = _cohort_reply_map(range_start, range_end, "tld", _ws(workspace_id))
        _merge_cohort_fields(rows, "tld", cohort)
    except Exception as e:
        logger.warning(f"cohort reply merge failed for tld-performance: {e}")
    return rows


@router.get("/senders/tag-performance", dependencies=[Security(require_api_key)])
def tag_performance(
    days: int = 30,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    workspace_id: Optional[str] = None,
):
    """Per-tag, per-day aggregates: sender_daily_stats joined to each sender's
    latest Bison tags (SM-GOOG-0609-Set1, …).

    Rows: {tag, stat_date, senders, emails_sent, emails_replied, emails_bounced,
    warmup_sent, daily_limit}.
    """
    range_start, range_end = _resolve_date_range(days, start_date, end_date)
    params: dict = {"p_start": range_start, "p_end": range_end}
    if _ws(workspace_id):
        params["p_workspace_id"] = _ws(workspace_id)
    try:
        # Paged: a year of per-tag daily rows exceeds the 1000-row cap.
        rows = fetch_all(lambda: get_supabase().rpc("get_tag_daily_performance", params))
    except Exception:
        raise
    # Bison mirrors each bundle tag with an internal "p."-prefixed copy
    # (p.CI-DED-Set4-0518, …) that duplicates the real tag — exclude them.
    rows = [r for r in rows if "p." not in (r.get("tag") or "")]
    try:
        cohort = _cohort_reply_map(range_start, range_end, "tag", _ws(workspace_id))
        _merge_cohort_fields(rows, "tag", cohort)
    except Exception as e:
        logger.warning(f"cohort reply merge failed for tag-performance: {e}")
    return rows


@router.get("/senders/{sender_email_id}/history", dependencies=[Security(require_api_key)])
def sender_history(
    sender_email_id: int,
    days: int = 30,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    """Per-day time-series stats for a single sender.

    Either pass `days` (last N days ending today) or an explicit `start_date` /
    `end_date` range (YYYY-MM-DD, inclusive). Explicit dates take precedence.

    Bison counters are cumulative lifetime snapshots, so each day's value is
    the delta from the previous snapshot (the first-ever snapshot reports 0 —
    its true daily value is unknown). Replies instead come from reply_events
    by arrival date, which is accurate per-day and unaffected by snapshot
    history gaps.
    """
    supabase = get_supabase()
    range_start, range_end = _resolve_date_range(days, start_date, end_date)
    try:
        rows = fetch_all(
            lambda: supabase.table("sender_daily_stats")
            .select("*")
            .eq("sender_email_id", sender_email_id)
            .lte("stat_date", range_end)
            .order("stat_date")
        )
        if not rows:
            raise HTTPException(status_code=404, detail="Sender not found or no data")

        # Bison sender ids collide across workspaces; keep the workspace with
        # the freshest snapshot so deltas aren't computed across mixed fleets.
        by_ws: dict = {}
        for r in rows:
            by_ws.setdefault(r.get("workspace_id"), []).append(r)
        history = max(by_ws.values(), key=lambda h: h[-1]["stat_date"])

        out: list[dict] = []
        prev: Optional[dict] = None
        for r in history:
            d = dict(r)
            for field in _DELTA_FIELDS:
                base = (prev or {}).get(field) or 0
                d[field] = max(0, (r.get(field) or 0) - base) if prev is not None else 0
            prev = r
            if range_start <= r["stat_date"] <= range_end:
                out.append(d)
        if not out:
            raise HTTPException(status_code=404, detail="Sender not found or no data")

        # Replies by arrival date (excludes automated replies).
        try:
            email = (history[-1].get("sender_email") or "").lower()
            if email:
                replies = fetch_all(
                    lambda: supabase.table("reply_events")
                    .select("reply_id,replied_at,classification")
                    .eq("sender_email", email)
                    .gte("replied_at", f"{range_start}T00:00:00Z")
                    .lte("replied_at", f"{range_end}T23:59:59Z")
                    .order("reply_id")
                )
                per_day: dict[str, int] = {}
                for rep in replies:
                    if rep.get("classification") == "automated_reply":
                        continue
                    day = str(rep.get("replied_at") or "")[:10]
                    per_day[day] = per_day.get(day, 0) + 1
                for d in out:
                    d["emails_replied"] = per_day.get(str(d["stat_date"])[:10], 0)
        except Exception as e:
            logger.warning(f"reply_events merge failed for sender history: {e}")

        return out
    except HTTPException:
        raise
    except Exception:
        raise


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
        rows = fetch_all(
            lambda: get_supabase()
            .table("sender_daily_stats")
            .select("sender_email_id,emails_sent,daily_limit")
            .eq("stat_date", _today())
            .in_("sender_email_id", sender_ids)
            .order("sender_email_id")
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
    since = (datetime.now(timezone.utc).date() - timedelta(days=30)).isoformat()
    try:
        rows = fetch_all(
            lambda: get_supabase()
            .table("sender_email_performance")
            .select("sender_email_id,snapshot_date,warmup_score,in_recovery,fetched_at")
            .in_("sender_email_id", sender_ids)
            .gte("snapshot_date", since)
            .order("sender_email_id")
            .order("snapshot_date", desc=True)
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


@router.get("/sender-performance", dependencies=[Security(require_api_key)])
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
        def build():
            query = (
                supabase.table("sender_email_performance")
                .select(_SENDER_PERF_COLS)
                .eq("snapshot_date", date)
                .order("health_score", desc=True)
                .order("sender_email_id")
            )
            if domain:
                query = query.eq("domain", domain)
            if in_recovery is not None:
                query = query.eq("in_recovery", in_recovery)
            return query

        return _enrich_sender_perf(fetch_all(build))
    except Exception:
        raise


@router.get("/sender-performance/{sender_email_id}", dependencies=[Security(require_api_key)])
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
    except Exception:
        raise


# ---------------------------------------------------------------------------
# Daily limits — capacity staging payload for the dashboard UI
# ---------------------------------------------------------------------------

def _prior_day_stats_map(ws_filter: Optional[str], today: str) -> dict[tuple, dict]:
    """Yesterday's (or most recent prior) emails_sent per sender for deltas."""
    try:
        today_d = datetime.fromisoformat(today).date()
    except ValueError:
        today_d = datetime.now(timezone.utc).date()
    prior_date = (today_d - timedelta(days=1)).isoformat()
    try:
        def build():
            query = (
                get_supabase()
                .table("sender_daily_stats")
                .select("workspace_id,sender_email_id,emails_sent,stat_date")
                .lt("stat_date", today)
                .gte("stat_date", (today_d - timedelta(days=7)).isoformat())
                .order("sender_email_id")
                .order("stat_date", desc=True)
            )
            if ws_filter:
                query = query.eq("workspace_id", ws_filter)
            return query

        rows = fetch_all(build)
    except Exception as e:
        logger.warning(f"prior day stats lookup failed: {e}")
        return {}

    out: dict[tuple, dict] = {}
    for r in rows:
        key = (r.get("workspace_id"), r.get("sender_email_id"))
        if key not in out:
            out[key] = r
    # Prefer exact yesterday when present (already first due to desc order).
    _ = prior_date
    return out


@router.get("/senders/daily-limits", dependencies=[Security(require_api_key)])
def sender_daily_limits(workspace_id: Optional[str] = None):
    """Fleet daily-limit capacity view: senders, tags, KPIs, Set 1–6 bundles.

    Built from the latest sender_daily_stats + tags from sender_email_performance.
    `sent_today` is a cumulative delta vs the prior snapshot (same as notifier).
    """
    from lib.daily_limits import (
        build_bundles,
        compute_kpis,
        normalize_sender_row,
        tag_counts,
    )

    ws_filter = _ws(workspace_id)
    today = _today()
    supabase = get_supabase()

    def build_today():
        query = (
            supabase.table("sender_daily_stats")
            .select(_SENDER_STATS_COLS)
            .eq("stat_date", today)
            .order("sender_email")
        )
        if ws_filter:
            query = query.eq("workspace_id", ws_filter)
        return query

    try:
        rows = fetch_all(build_today)
        if not rows:
            params: dict = {}
            if ws_filter:
                params["p_workspace_id"] = ws_filter
            rows = fetch_all(lambda: supabase.rpc("get_latest_sender_stats", params))
    except Exception:
        raise

    prior = _prior_day_stats_map(ws_filter, today)
    perf_map = _latest_perf_map()

    senders: list[dict] = []
    for row in rows or []:
        sid = row.get("sender_email_id")
        perf = perf_map.get(int(sid), {}) if sid is not None else {}
        row = dict(row)
        row["tags"] = perf.get("tags", [])
        key = (row.get("workspace_id"), sid)
        prev = prior.get(key)
        if prev is None:
            sent_today = None
        else:
            sent_today = max(
                (row.get("emails_sent") or 0) - (prev.get("emails_sent") or 0), 0
            )
        senders.append(normalize_sender_row(row, sent_today=sent_today))

    return {
        "kpis": compute_kpis(senders),
        "tags": tag_counts(senders),
        "bundles": build_bundles(senders),
        "senders": senders,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


