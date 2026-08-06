"""Warmup fleet report — Slack / bison-reports style buckets.

Bucket semantics (match Saman's Bison Report Bot / bison-reports.vercel.app):
- total_accounts: every sender row in the snapshot for the date/workspace
- not_warming: warmup_enabled is false or null (status, not a score band)
- not_connected: connection_status present and not "Connected"
- score bands among accounts that are warming (enabled):
  - score_95_plus:  score >= 95
  - score_90_to_94: 90 <= score < 95
  - score_below_90: 0 < score < 90
  - never_warmed: score == 0 (warming but zero warmup history)
- Enabled but unscored accounts stay in total_accounts only.
  Percentages are each count / total_accounts.

Also surfaces: not-warming / never-warmed account lists, below-threshold
rows, per-tag (set) health, fleet averages, previous-day deltas, and
available archive dates.

Internal Bison tags containing "p." are stripped (same rule as senders/notifier).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from lib.supabase_client import get_supabase
from lib.supabase_paginate import fetch_all

logger = logging.getLogger(__name__)

_PERF_COLS = (
    "workspace_id,sender_email_id,sender_email,domain,"
    "warmup_enabled,warmup_score,tags,connection_status"
)

_COUNT_KEYS = (
    "total",
    "not_warming",
    "not_connected",
    "score_95_plus",
    "score_90_to_94",
    "score_below_90",
    "never_warmed",
)

_HEADLINE_KEYS = (
    "not_warming",
    "not_connected",
    "score_95_plus",
    "score_90_to_94",
    "score_below_90",
    "never_warmed",
)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def normalize_workspace_id(workspace_id: Optional[str]) -> Optional[str]:
    """None / 'all' → no filter (aggregate all workspaces)."""
    if workspace_id and workspace_id != "all":
        return workspace_id
    return None


def normalize_tags(raw) -> list[str]:
    """Clean tag names; drop internal Bison copies that contain 'p.'."""
    if not raw:
        return []
    tags: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            tags.append(item.strip())
        elif isinstance(item, dict) and item.get("name"):
            tags.append(str(item["name"]).strip())
    return [t for t in tags if "p." not in t]


def _is_warming(row: dict) -> bool:
    return bool(row.get("warmup_enabled"))


def _is_disconnected(row: dict) -> bool:
    status = row.get("connection_status")
    if status is None or str(status).strip() == "":
        return False
    return str(status).strip().lower() != "connected"


def _parse_score(score: Any) -> Optional[float]:
    if score is None:
        return None
    try:
        return float(score)
    except (TypeError, ValueError):
        return None


def _score_bucket(score: Any) -> Optional[str]:
    """Map a numeric score to a bucket key, or None if unscored."""
    s = _parse_score(score)
    if s is None:
        return None
    if s == 0:
        return "never_warmed"
    if s >= 95:
        return "score_95_plus"
    if s >= 90:
        return "score_90_to_94"
    return "score_below_90"


def _empty_counts() -> dict[str, int]:
    return {k: 0 for k in _COUNT_KEYS}


def _pct(n: int, total: int) -> float:
    return round((n / total) * 100, 2) if total else 0.0


def _account_row(row: dict, tags: list[str], score_out: Optional[float]) -> dict:
    email = row.get("sender_email") or row.get("email") or ""
    domain = row.get("domain") or (email.split("@")[1] if "@" in email else "")
    return {
        "sender_email_id": str(row.get("sender_email_id")),
        "email": email,
        "domain": domain,
        "warmup_score": score_out,
        "tags": tags,
        "connection_status": row.get("connection_status"),
    }


def _score_out(score: Any) -> Optional[float]:
    s = _parse_score(score)
    if s is None:
        return None
    # Preserve one decimal when present; ints stay ints via round trip.
    return round(s, 2) if s != int(s) else int(s)


def build_report_from_rows(
    rows: list[dict],
    *,
    report_date: str,
    workspace_id: Optional[str],
    generated_at: Optional[str] = None,
    source: str = "live",
) -> dict:
    """Compute the warmup report dict from sender_email_performance rows.

    Pure function — no I/O. Safe for unit tests.
    """
    counts = _empty_counts()
    below_threshold: list[dict] = []
    not_warming_accounts: list[dict] = []
    never_warmed_accounts: list[dict] = []
    by_tag: dict[str, dict[str, Any]] = {}

    all_scores: list[float] = []
    active_scores: list[float] = []  # warming with score > 0
    perfect_100 = 0
    lowest_email: Optional[str] = None
    lowest_score: Optional[float] = None

    for row in rows:
        counts["total"] += 1
        tags = normalize_tags(row.get("tags"))
        warming = _is_warming(row)
        score = row.get("warmup_score")
        parsed = _parse_score(score)
        score_out = _score_out(score)
        disconnected = _is_disconnected(row)
        if disconnected:
            counts["not_connected"] += 1

        if parsed is not None:
            all_scores.append(parsed)

        bucket: Optional[str] = None
        if not warming:
            counts["not_warming"] += 1
            account_bucket = "not_warming"
            not_warming_accounts.append(_account_row(row, tags, score_out))
        else:
            bucket = _score_bucket(score)
            if bucket:
                counts[bucket] += 1
            account_bucket = bucket  # may be None if enabled+unscored

            if parsed is not None and parsed > 0:
                active_scores.append(parsed)
                if parsed >= 100:
                    perfect_100 += 1
                if lowest_score is None or parsed < lowest_score:
                    lowest_score = parsed
                    email = row.get("sender_email") or row.get("email") or ""
                    lowest_email = email

            if bucket == "score_below_90":
                below_threshold.append(_account_row(row, tags, score_out))
            elif bucket == "never_warmed":
                never_warmed_accounts.append(_account_row(row, tags, score_out))

        tag_names = tags if tags else ["untagged"]
        for tag in tag_names:
            tc = by_tag.setdefault(
                tag,
                {
                    **_empty_counts(),
                    "_score_sum": 0.0,
                    "_score_n": 0,
                    "_lowest": None,
                },
            )
            tc["total"] += 1
            if account_bucket:
                tc[account_bucket] += 1
            if disconnected:
                tc["not_connected"] += 1
            if parsed is not None:
                tc["_score_sum"] += parsed
                tc["_score_n"] += 1
                if tc["_lowest"] is None or parsed < tc["_lowest"]:
                    tc["_lowest"] = parsed

    total = counts["total"]
    below_threshold.sort(
        key=lambda r: (
            r.get("warmup_score") is None,
            r.get("warmup_score") or 0,
            r.get("email") or "",
        )
    )
    not_warming_accounts.sort(key=lambda r: r.get("email") or "")
    never_warmed_accounts.sort(key=lambda r: r.get("email") or "")

    by_tag_list = []
    for tag, c in sorted(by_tag.items(), key=lambda kv: kv[0].lower()):
        n_scores = int(c.pop("_score_n") or 0)
        score_sum = float(c.pop("_score_sum") or 0.0)
        lowest = c.pop("_lowest")
        below_95 = (
            int(c["score_90_to_94"])
            + int(c["score_below_90"])
            + int(c["never_warmed"])
        )
        by_tag_list.append(
            {
                "tag": tag,
                "total": c["total"],
                "not_warming": c["not_warming"],
                "not_connected": c["not_connected"],
                "score_95_plus": c["score_95_plus"],
                "score_90_to_94": c["score_90_to_94"],
                "score_below_90": c["score_below_90"],
                "never_warmed": c["never_warmed"],
                "below_95": below_95,
                "avg_score": round(score_sum / n_scores, 2) if n_scores else None,
                "lowest_score": round(lowest, 2) if lowest is not None else None,
            }
        )

    score_below_95 = (
        counts["score_90_to_94"] + counts["score_below_90"] + counts["never_warmed"]
    )
    generated = generated_at or datetime.now(timezone.utc).isoformat()
    return {
        "date": report_date,
        "generated_at": generated,
        "workspace_id": workspace_id,
        "total_accounts": total,
        "not_warming": counts["not_warming"],
        "not_connected": counts["not_connected"],
        "score_95_plus": counts["score_95_plus"],
        "score_90_to_94": counts["score_90_to_94"],
        "score_below_90": counts["score_below_90"],
        "never_warmed": counts["never_warmed"],
        "score_below_95": score_below_95,
        "percentages": {
            "not_warming": _pct(counts["not_warming"], total),
            "not_connected": _pct(counts["not_connected"], total),
            "score_95_plus": _pct(counts["score_95_plus"], total),
            "score_90_to_94": _pct(counts["score_90_to_94"], total),
            "score_below_90": _pct(counts["score_below_90"], total),
            "never_warmed": _pct(counts["never_warmed"], total),
            "score_below_95": _pct(score_below_95, total),
        },
        "stats": {
            "avg_score_all": round(sum(all_scores) / len(all_scores), 2) if all_scores else None,
            "avg_score_active": (
                round(sum(active_scores) / len(active_scores), 2) if active_scores else None
            ),
            "lowest_score": round(lowest_score, 2) if lowest_score is not None else None,
            "lowest_email": lowest_email,
            "perfect_100": perfect_100,
            "active_scored": len(active_scores),
        },
        "below_threshold": below_threshold,
        "not_warming_accounts": not_warming_accounts,
        "never_warmed_accounts": never_warmed_accounts,
        "by_tag": by_tag_list,
        "source": source,
        "previous": None,
        "delta": None,
    }


def fetch_performance_rows(
    report_date: str,
    workspace_id: Optional[str] = None,
    supabase=None,
) -> list[dict]:
    """All sender_email_performance rows for a snapshot date (paged)."""
    sb = supabase or get_supabase()
    ws = normalize_workspace_id(workspace_id)

    def _query():
        q = (
            sb.table("sender_email_performance")
            .select(_PERF_COLS)
            .eq("snapshot_date", report_date)
            .order("workspace_id")
            .order("sender_email_id")
        )
        if ws:
            q = q.eq("workspace_id", ws)
        return q

    try:
        return fetch_all(_query)
    except Exception as e:
        logger.error(f"Failed to fetch sender_email_performance for {report_date}: {e}")
        return []


def persist_warmup_daily_report(
    workspace_id: str,
    report_date: str,
    rows: list[dict] | None = None,
    supabase=None,
) -> dict | None:
    """Build + upsert today's warmup_daily_report for one workspace.

    If rows are provided (e.g. just written by the poller), use those;
    otherwise load from sender_email_performance for the date.
    """
    sb = supabase or get_supabase()
    if rows is None:
        rows = fetch_performance_rows(report_date, workspace_id, supabase=sb)
    if not rows:
        logger.info(f"[{workspace_id}] No performance rows for {report_date} — skip warmup report")
        return None

    captured_at = datetime.now(timezone.utc).isoformat()
    report = build_report_from_rows(
        rows,
        report_date=report_date,
        workspace_id=workspace_id,
        generated_at=captured_at,
        source="snapshot",
    )
    payload = {
        "below_threshold": report["below_threshold"],
        "not_warming_accounts": report["not_warming_accounts"],
        "never_warmed_accounts": report["never_warmed_accounts"],
        "by_tag": report["by_tag"],
        "percentages": report["percentages"],
        "stats": report["stats"],
        "not_connected": report["not_connected"],
        "never_warmed": report["never_warmed"],
        "score_below_95": report["score_below_95"],
    }
    row = {
        "workspace_id": workspace_id,
        "report_date": report_date,
        "total_accounts": report["total_accounts"],
        "not_warming": report["not_warming"],
        "score_95_plus": report["score_95_plus"],
        "score_90_to_94": report["score_90_to_94"],
        "score_below_90": report["score_below_90"],
        "payload": payload,
        "captured_at": captured_at,
    }
    try:
        sb.table("warmup_daily_report").upsert(
            row, on_conflict="workspace_id,report_date"
        ).execute()
        logger.info(
            f"[{workspace_id}] Upserted warmup_daily_report for {report_date}: "
            f"{report['total_accounts']} accounts"
        )
    except Exception as e:
        logger.error(f"[{workspace_id}] Failed to upsert warmup_daily_report: {e}")
        return None
    return report


def _merge_reports(parts: list[dict], *, report_date: str, source: str) -> dict:
    """Aggregate per-workspace reports into one fleet view."""
    if len(parts) == 1:
        out = dict(parts[0])
        out["workspace_id"] = None
        out["source"] = source
        out["date"] = report_date
        out.setdefault("previous", None)
        out.setdefault("delta", None)
        return out

    total = sum(p.get("total_accounts") or 0 for p in parts)
    not_warming = sum(p.get("not_warming") or 0 for p in parts)
    not_connected = sum(p.get("not_connected") or 0 for p in parts)
    score_95 = sum(p.get("score_95_plus") or 0 for p in parts)
    score_90 = sum(p.get("score_90_to_94") or 0 for p in parts)
    score_below = sum(p.get("score_below_90") or 0 for p in parts)
    never_warmed = sum(p.get("never_warmed") or 0 for p in parts)
    score_below_95 = score_90 + score_below + never_warmed

    below: list[dict] = []
    not_warming_accounts: list[dict] = []
    never_warmed_accounts: list[dict] = []
    tag_map: dict[str, dict[str, Any]] = {}
    generated_ats: list[str] = []

    # Weighted averages from per-workspace stats when present.
    score_sum_all = 0.0
    score_n_all = 0
    score_sum_active = 0.0
    score_n_active = 0
    perfect_100 = 0
    lowest_score: Optional[float] = None
    lowest_email: Optional[str] = None

    for p in parts:
        if p.get("generated_at"):
            generated_ats.append(str(p["generated_at"]))
        below.extend(p.get("below_threshold") or [])
        not_warming_accounts.extend(p.get("not_warming_accounts") or [])
        never_warmed_accounts.extend(p.get("never_warmed_accounts") or [])

        stats = p.get("stats") or {}
        n_all = int(p.get("total_accounts") or 0)
        avg_all = stats.get("avg_score_all")
        if avg_all is not None and n_all:
            # Approximate: weight by total accounts (close enough for fleet view).
            score_sum_all += float(avg_all) * n_all
            score_n_all += n_all
        n_active = int(stats.get("active_scored") or 0)
        avg_active = stats.get("avg_score_active")
        if avg_active is not None and n_active:
            score_sum_active += float(avg_active) * n_active
            score_n_active += n_active
        perfect_100 += int(stats.get("perfect_100") or 0)
        ls = stats.get("lowest_score")
        if ls is not None and (lowest_score is None or float(ls) < lowest_score):
            lowest_score = float(ls)
            lowest_email = stats.get("lowest_email")

        for t in p.get("by_tag") or []:
            tag = t.get("tag")
            if not tag:
                continue
            tc = tag_map.setdefault(
                tag,
                {
                    **_empty_counts(),
                    "below_95": 0,
                    "_score_sum": 0.0,
                    "_score_n": 0,
                    "_lowest": None,
                },
            )
            for k in (
                "total",
                "not_warming",
                "not_connected",
                "score_95_plus",
                "score_90_to_94",
                "score_below_90",
                "never_warmed",
                "below_95",
            ):
                tc[k] = int(tc.get(k) or 0) + int(t.get(k) or 0)
            if t.get("avg_score") is not None and t.get("total"):
                tc["_score_sum"] += float(t["avg_score"]) * int(t["total"])
                tc["_score_n"] += int(t["total"])
            if t.get("lowest_score") is not None:
                cur = tc["_lowest"]
                if cur is None or float(t["lowest_score"]) < cur:
                    tc["_lowest"] = float(t["lowest_score"])

    below.sort(
        key=lambda r: (
            r.get("warmup_score") is None,
            r.get("warmup_score") or 0,
            r.get("email") or "",
        )
    )
    not_warming_accounts.sort(key=lambda r: r.get("email") or "")
    never_warmed_accounts.sort(key=lambda r: r.get("email") or "")

    by_tag_list = []
    for tag, c in sorted(tag_map.items(), key=lambda kv: kv[0].lower()):
        n_scores = int(c.pop("_score_n") or 0)
        score_sum = float(c.pop("_score_sum") or 0.0)
        lowest = c.pop("_lowest")
        by_tag_list.append(
            {
                "tag": tag,
                "total": c["total"],
                "not_warming": c["not_warming"],
                "not_connected": c.get("not_connected") or 0,
                "score_95_plus": c["score_95_plus"],
                "score_90_to_94": c["score_90_to_94"],
                "score_below_90": c["score_below_90"],
                "never_warmed": c.get("never_warmed") or 0,
                "below_95": c.get("below_95")
                or (
                    int(c["score_90_to_94"])
                    + int(c["score_below_90"])
                    + int(c.get("never_warmed") or 0)
                ),
                "avg_score": round(score_sum / n_scores, 2) if n_scores else None,
                "lowest_score": round(lowest, 2) if lowest is not None else None,
            }
        )

    generated_at = max(generated_ats) if generated_ats else datetime.now(timezone.utc).isoformat()
    return {
        "date": report_date,
        "generated_at": generated_at,
        "workspace_id": None,
        "total_accounts": total,
        "not_warming": not_warming,
        "not_connected": not_connected,
        "score_95_plus": score_95,
        "score_90_to_94": score_90,
        "score_below_90": score_below,
        "never_warmed": never_warmed,
        "score_below_95": score_below_95,
        "percentages": {
            "not_warming": _pct(not_warming, total),
            "not_connected": _pct(not_connected, total),
            "score_95_plus": _pct(score_95, total),
            "score_90_to_94": _pct(score_90, total),
            "score_below_90": _pct(score_below, total),
            "never_warmed": _pct(never_warmed, total),
            "score_below_95": _pct(score_below_95, total),
        },
        "stats": {
            "avg_score_all": round(score_sum_all / score_n_all, 2) if score_n_all else None,
            "avg_score_active": (
                round(score_sum_active / score_n_active, 2) if score_n_active else None
            ),
            "lowest_score": round(lowest_score, 2) if lowest_score is not None else None,
            "lowest_email": lowest_email,
            "perfect_100": perfect_100,
            "active_scored": score_n_active,
        },
        "below_threshold": below,
        "not_warming_accounts": not_warming_accounts,
        "never_warmed_accounts": never_warmed_accounts,
        "by_tag": by_tag_list,
        "source": source,
        "previous": None,
        "delta": None,
    }


def _report_from_snapshot_row(row: dict) -> dict:
    payload = row.get("payload") or {}
    if isinstance(payload, str):
        import json
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    total = int(row.get("total_accounts") or 0)
    not_warming = int(row.get("not_warming") or 0)
    score_95 = int(row.get("score_95_plus") or 0)
    score_90 = int(row.get("score_90_to_94") or 0)
    score_below = int(row.get("score_below_90") or 0)
    never_warmed = int(payload.get("never_warmed") or 0)
    not_connected = int(payload.get("not_connected") or 0)
    # Older snapshots stored never_warmed inside below_90 — leave as-is.
    score_below_95 = int(
        payload.get("score_below_95")
        if payload.get("score_below_95") is not None
        else score_90 + score_below + never_warmed
    )
    percentages = payload.get("percentages") or {
        "not_warming": _pct(not_warming, total),
        "not_connected": _pct(not_connected, total),
        "score_95_plus": _pct(score_95, total),
        "score_90_to_94": _pct(score_90, total),
        "score_below_90": _pct(score_below, total),
        "never_warmed": _pct(never_warmed, total),
        "score_below_95": _pct(score_below_95, total),
    }
    return {
        "date": str(row.get("report_date")),
        "generated_at": str(row.get("captured_at") or ""),
        "workspace_id": row.get("workspace_id"),
        "total_accounts": total,
        "not_warming": not_warming,
        "not_connected": not_connected,
        "score_95_plus": score_95,
        "score_90_to_94": score_90,
        "score_below_90": score_below,
        "never_warmed": never_warmed,
        "score_below_95": score_below_95,
        "percentages": percentages,
        "stats": payload.get("stats") or {
            "avg_score_all": None,
            "avg_score_active": None,
            "lowest_score": None,
            "lowest_email": None,
            "perfect_100": 0,
            "active_scored": 0,
        },
        "below_threshold": payload.get("below_threshold") or [],
        "not_warming_accounts": payload.get("not_warming_accounts") or [],
        "never_warmed_accounts": payload.get("never_warmed_accounts") or [],
        "by_tag": payload.get("by_tag") or [],
        "source": "snapshot",
        "previous": None,
        "delta": None,
    }


def fetch_snapshots(
    report_date: str,
    workspace_id: Optional[str] = None,
    supabase=None,
) -> list[dict]:
    sb = supabase or get_supabase()
    ws = normalize_workspace_id(workspace_id)
    try:
        q = (
            sb.table("warmup_daily_report")
            .select("*")
            .eq("report_date", report_date)
        )
        if ws:
            q = q.eq("workspace_id", ws)
        return q.execute().data or []
    except Exception as e:
        logger.error(f"Failed to fetch warmup_daily_report for {report_date}: {e}")
        return []


def _headline_slice(report: dict) -> dict:
    return {
        "date": report.get("date"),
        "total_accounts": report.get("total_accounts") or 0,
        "not_warming": report.get("not_warming") or 0,
        "not_connected": report.get("not_connected") or 0,
        "score_95_plus": report.get("score_95_plus") or 0,
        "score_90_to_94": report.get("score_90_to_94") or 0,
        "score_below_90": report.get("score_below_90") or 0,
        "never_warmed": report.get("never_warmed") or 0,
        "score_below_95": report.get("score_below_95") or 0,
    }


def _attach_previous(report: dict, previous: Optional[dict]) -> dict:
    if not previous or (previous.get("total_accounts") or 0) == 0:
        report["previous"] = None
        report["delta"] = None
        return report
    prev = _headline_slice(previous)
    report["previous"] = prev
    report["delta"] = {
        k: int(report.get(k) or 0) - int(prev.get(k) or 0)
        for k in _HEADLINE_KEYS
    }
    report["delta"]["score_below_95"] = int(report.get("score_below_95") or 0) - int(
        prev.get("score_below_95") or 0
    )
    return report


def _compute_report_for_date(
    report_date: str,
    workspace_id: Optional[str],
    supabase,
) -> dict:
    """Live today from performance; historical from snapshot (with fallback)."""
    ws = normalize_workspace_id(workspace_id)
    today = _today()

    if report_date == today:
        rows = fetch_performance_rows(report_date, ws, supabase=supabase)
        return build_report_from_rows(
            rows,
            report_date=report_date,
            workspace_id=ws,
            source="live",
        )

    snap_rows = fetch_snapshots(report_date, ws, supabase=supabase)
    if not snap_rows:
        rows = fetch_performance_rows(report_date, ws, supabase=supabase)
        if rows:
            return build_report_from_rows(
                rows,
                report_date=report_date,
                workspace_id=ws,
                source="live",
            )
        return build_report_from_rows(
            [],
            report_date=report_date,
            workspace_id=ws,
            source="snapshot",
        )

    parts = [_report_from_snapshot_row(r) for r in snap_rows]
    if ws:
        out = parts[0]
        out["workspace_id"] = ws
        return out
    return _merge_reports(parts, report_date=report_date, source="snapshot")


def get_warmup_report(
    workspace_id: Optional[str] = None,
    date: Optional[str] = None,
    supabase=None,
) -> dict:
    """Serve live (today) or snapshot (historical) warmup report with day delta.

    Today: compute from sender_email_performance (source=live).
    Historical: serve from warmup_daily_report (source=snapshot).
    Omit/all workspace_id aggregates V1+V2.
    Always attaches previous-day headline counts when available.
    """
    sb = supabase or get_supabase()
    report_date = date or _today()
    report = _compute_report_for_date(report_date, workspace_id, sb)

    try:
        prev_date = (
            datetime.strptime(report_date, "%Y-%m-%d").date() - timedelta(days=1)
        ).isoformat()
        previous = _compute_report_for_date(prev_date, workspace_id, sb)
        _attach_previous(report, previous)
    except Exception as e:
        logger.warning(f"Failed to attach previous-day warmup delta: {e}")
        report["previous"] = None
        report["delta"] = None

    return report


def list_available_dates(
    workspace_id: Optional[str] = None,
    *,
    keep_days: int = 90,
    supabase=None,
) -> list[dict]:
    """Archive dates for the warmup day picker (newest first).

    Prefers warmup_daily_report snapshots; also includes distinct
    sender_email_performance snapshot_date values so the picker works
    before migration 017 is applied.
    """
    sb = supabase or get_supabase()
    ws = normalize_workspace_id(workspace_id)
    since = (
        datetime.now(timezone.utc).date() - timedelta(days=keep_days - 1)
    ).isoformat()
    by_date: dict[str, dict] = {}

    try:
        q = (
            sb.table("warmup_daily_report")
            .select(
                "report_date,total_accounts,not_warming,"
                "score_95_plus,score_90_to_94,score_below_90,payload"
            )
            .gte("report_date", since)
            .order("report_date", desc=True)
        )
        if ws:
            q = q.eq("workspace_id", ws)
        rows = q.execute().data or []
        for r in rows:
            d = str(r.get("report_date"))
            payload = r.get("payload") or {}
            if isinstance(payload, str):
                import json
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            cur = by_date.setdefault(
                d,
                {
                    "date": d,
                    "total": 0,
                    "good": 0,
                    "watch": 0,
                    "below90": 0,
                    "never_warmed": 0,
                    "notwarming": 0,
                    "disconnected": 0,
                },
            )
            cur["total"] += int(r.get("total_accounts") or 0)
            cur["good"] += int(r.get("score_95_plus") or 0)
            cur["watch"] += int(r.get("score_90_to_94") or 0)
            cur["below90"] += int(r.get("score_below_90") or 0) + int(
                payload.get("never_warmed") or 0
            )
            cur["never_warmed"] += int(payload.get("never_warmed") or 0)
            cur["notwarming"] += int(r.get("not_warming") or 0)
            cur["disconnected"] += int(payload.get("not_connected") or 0)
            stats = payload.get("stats") or {}
            if stats.get("avg_score_active") is not None:
                cur["avg_active"] = stats.get("avg_score_active")
    except Exception as e:
        logger.warning(f"warmup_daily_report date list failed: {e}")

    # Performance table fallback / supplement (covers today before snapshot).
    try:
        q = (
            sb.table("sender_email_performance")
            .select("snapshot_date")
            .gte("snapshot_date", since)
            .order("snapshot_date", desc=True)
        )
        if ws:
            q = q.eq("workspace_id", ws)
        # Distinct isn't available on all PostgREST builds — de-dupe in Python.
        rows = q.limit(5000).execute().data or []
        for r in rows:
            d = str(r.get("snapshot_date"))
            if d and d not in by_date:
                by_date[d] = {"date": d}
    except Exception as e:
        logger.warning(f"sender_email_performance date list failed: {e}")

    today = _today()
    if today not in by_date:
        by_date[today] = {"date": today}

    return sorted(by_date.values(), key=lambda d: d["date"], reverse=True)
