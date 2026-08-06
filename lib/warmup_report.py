"""Warmup fleet report — Slack-style buckets from sender_email_performance.

Bucket semantics (match Saman's Bison Report Bot as closely as practical):
- total_accounts: every sender row in the snapshot for the date/workspace
- not_warming: warmup_enabled is false or null
- score buckets: among accounts that are warming (enabled) AND have a score
  - score_95_plus:  score >= 95
  - score_90_to_94: 90 <= score < 95  (Slack label "95-90")
  - score_below_90: score < 90
- Enabled but unscored accounts stay in total_accounts only (not in the
  four headline buckets). Percentages are each count / total_accounts.

Internal Bison tags containing "p." are stripped (same rule as senders/notifier).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from lib.supabase_client import get_supabase
from lib.supabase_paginate import fetch_all

logger = logging.getLogger(__name__)

_PERF_COLS = (
    "workspace_id,sender_email_id,sender_email,domain,"
    "warmup_enabled,warmup_score,tags"
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


def _score_bucket(score: Any) -> Optional[str]:
    """Map a numeric score to a bucket key, or None if unscored."""
    if score is None:
        return None
    try:
        s = float(score)
    except (TypeError, ValueError):
        return None
    if s >= 95:
        return "score_95_plus"
    if s >= 90:
        return "score_90_to_94"
    return "score_below_90"


def _empty_counts() -> dict[str, int]:
    return {
        "total": 0,
        "not_warming": 0,
        "score_95_plus": 0,
        "score_90_to_94": 0,
        "score_below_90": 0,
    }


def _pct(n: int, total: int) -> float:
    return round((n / total) * 100, 2) if total else 0.0


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
    by_tag: dict[str, dict[str, int]] = {}

    for row in rows:
        counts["total"] += 1
        tags = normalize_tags(row.get("tags"))
        warming = _is_warming(row)
        score = row.get("warmup_score")
        bucket: Optional[str] = None

        if not warming:
            counts["not_warming"] += 1
            account_bucket = "not_warming"
        else:
            bucket = _score_bucket(score)
            if bucket:
                counts[bucket] += 1
            account_bucket = bucket  # may be None if enabled+unscored

            if bucket == "score_below_90":
                email = row.get("sender_email") or row.get("email") or ""
                domain = row.get("domain") or (
                    email.split("@")[1] if "@" in email else ""
                )
                try:
                    score_out = round(float(score)) if score is not None else None
                except (TypeError, ValueError):
                    score_out = None
                below_threshold.append({
                    "sender_email_id": str(row.get("sender_email_id")),
                    "email": email,
                    "domain": domain,
                    "warmup_score": score_out,
                    "tags": tags,
                })

        for tag in tags:
            tc = by_tag.setdefault(tag, _empty_counts())
            tc["total"] += 1
            if account_bucket:
                tc[account_bucket] += 1

    total = counts["total"]
    below_threshold.sort(
        key=lambda r: (r.get("warmup_score") is None, r.get("warmup_score") or 0, r.get("email") or "")
    )
    by_tag_list = [
        {
            "tag": tag,
            "total": c["total"],
            "not_warming": c["not_warming"],
            "score_95_plus": c["score_95_plus"],
            "score_90_to_94": c["score_90_to_94"],
            "score_below_90": c["score_below_90"],
        }
        for tag, c in sorted(by_tag.items(), key=lambda kv: kv[0].lower())
    ]

    generated = generated_at or datetime.now(timezone.utc).isoformat()
    return {
        "date": report_date,
        "generated_at": generated,
        "workspace_id": workspace_id,
        "total_accounts": total,
        "not_warming": counts["not_warming"],
        "score_95_plus": counts["score_95_plus"],
        "score_90_to_94": counts["score_90_to_94"],
        "score_below_90": counts["score_below_90"],
        "percentages": {
            "not_warming": _pct(counts["not_warming"], total),
            "score_95_plus": _pct(counts["score_95_plus"], total),
            "score_90_to_94": _pct(counts["score_90_to_94"], total),
            "score_below_90": _pct(counts["score_below_90"], total),
        },
        "below_threshold": below_threshold,
        "by_tag": by_tag_list,
        "source": source,
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
        "by_tag": report["by_tag"],
        "percentages": report["percentages"],
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
        return out

    # Snapshot merge: sum headline counts and merge detail lists.
    total = sum(p.get("total_accounts") or 0 for p in parts)
    not_warming = sum(p.get("not_warming") or 0 for p in parts)
    score_95 = sum(p.get("score_95_plus") or 0 for p in parts)
    score_90 = sum(p.get("score_90_to_94") or 0 for p in parts)
    score_below = sum(p.get("score_below_90") or 0 for p in parts)

    below: list[dict] = []
    tag_map: dict[str, dict[str, int]] = {}
    generated_ats: list[str] = []
    for p in parts:
        if p.get("generated_at"):
            generated_ats.append(str(p["generated_at"]))
        below.extend(p.get("below_threshold") or [])
        for t in p.get("by_tag") or []:
            tag = t.get("tag")
            if not tag:
                continue
            tc = tag_map.setdefault(tag, _empty_counts())
            for k in ("total", "not_warming", "score_95_plus", "score_90_to_94", "score_below_90"):
                tc[k] += int(t.get(k) or 0)

    below.sort(
        key=lambda r: (r.get("warmup_score") is None, r.get("warmup_score") or 0, r.get("email") or "")
    )
    by_tag_list = [
        {
            "tag": tag,
            "total": c["total"],
            "not_warming": c["not_warming"],
            "score_95_plus": c["score_95_plus"],
            "score_90_to_94": c["score_90_to_94"],
            "score_below_90": c["score_below_90"],
        }
        for tag, c in sorted(tag_map.items(), key=lambda kv: kv[0].lower())
    ]
    generated_at = max(generated_ats) if generated_ats else datetime.now(timezone.utc).isoformat()
    return {
        "date": report_date,
        "generated_at": generated_at,
        "workspace_id": None,
        "total_accounts": total,
        "not_warming": not_warming,
        "score_95_plus": score_95,
        "score_90_to_94": score_90,
        "score_below_90": score_below,
        "percentages": {
            "not_warming": _pct(not_warming, total),
            "score_95_plus": _pct(score_95, total),
            "score_90_to_94": _pct(score_90, total),
            "score_below_90": _pct(score_below, total),
        },
        "below_threshold": below,
        "by_tag": by_tag_list,
        "source": source,
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
    percentages = payload.get("percentages") or {
        "not_warming": _pct(not_warming, total),
        "score_95_plus": _pct(score_95, total),
        "score_90_to_94": _pct(score_90, total),
        "score_below_90": _pct(score_below, total),
    }
    return {
        "date": str(row.get("report_date")),
        "generated_at": str(row.get("captured_at") or ""),
        "workspace_id": row.get("workspace_id"),
        "total_accounts": total,
        "not_warming": not_warming,
        "score_95_plus": score_95,
        "score_90_to_94": score_90,
        "score_below_90": score_below,
        "percentages": percentages,
        "below_threshold": payload.get("below_threshold") or [],
        "by_tag": payload.get("by_tag") or [],
        "source": "snapshot",
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


def get_warmup_report(
    workspace_id: Optional[str] = None,
    date: Optional[str] = None,
    supabase=None,
) -> dict:
    """Serve live (today) or snapshot (historical) warmup report.

    Today: compute from sender_email_performance (source=live).
    Historical: serve from warmup_daily_report (source=snapshot).
    Omit/all workspace_id aggregates V1+V2.
    """
    sb = supabase or get_supabase()
    report_date = date or _today()
    ws = normalize_workspace_id(workspace_id)
    today = _today()

    if report_date == today:
        rows = fetch_performance_rows(report_date, ws, supabase=sb)
        return build_report_from_rows(
            rows,
            report_date=report_date,
            workspace_id=ws,
            source="live",
        )

    snap_rows = fetch_snapshots(report_date, ws, supabase=sb)
    if not snap_rows:
        # Fall back to performance table if a historical snapshot is missing
        # but rows still exist (e.g. migration applied mid-day).
        rows = fetch_performance_rows(report_date, ws, supabase=sb)
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
