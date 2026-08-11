"""Sender daily-limit capacity helpers — set parsing, utilization KPIs, bundles.

Used by GET /senders/daily-limits and the dashboard staging UI. Pure functions
so unit tests don't need Supabase/EmailBison.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Iterable, Optional

from lib.warmup_report import normalize_tags

# Heatmap columns in the Bundles-by-daily-limit grid.
SET_COLUMNS = (1, 2, 3, 4, 5, 6)

# Tags like SM-GOOG-0609-Set1, CI-DED-Set4, SET3, Set 2
_SET_RE = re.compile(r"(?i)(?:^|[-_\s])set\s*([1-6])(?=$|[-_\s])")


def extract_set_indexes(tags: Iterable[str]) -> list[int]:
    """Return sorted unique Set 1–6 indexes found in tag names."""
    found: set[int] = set()
    for tag in tags:
        if not isinstance(tag, str):
            continue
        for match in _SET_RE.finditer(tag):
            found.add(int(match.group(1)))
    return sorted(found)


def utilization(sent_today: Optional[int], daily_limit: int) -> Optional[float]:
    """sent_today / daily_limit, or None when volume is unknown / limit is 0."""
    if sent_today is None or daily_limit <= 0:
        return None
    return sent_today / daily_limit


def compute_kpis(senders: list[dict]) -> dict[str, int]:
    """Fleet capacity + utilization KPIs for the daily-limits strip."""
    total_daily_limit = 0
    at_limit = 0
    settling = 0
    over_50 = 0
    bundled = 0

    for row in senders:
        limit = int(row.get("daily_limit") or 0)
        total_daily_limit += limit
        tags = row.get("tags") or []
        if tags:
            bundled += 1
        util = utilization(row.get("sent_today"), limit)
        if util is None:
            continue
        if util >= 1.0:
            at_limit += 1
        elif util >= 0.95:
            settling += 1
        if util > 0.5:
            over_50 += 1

    return {
        "total_daily_limit": total_daily_limit,
        "senders_at_limit": at_limit,
        "settling_users": settling,
        "senders_over_50": over_50,
        "sender_count": len(senders),
        "bundled_sender_count": bundled,
    }


def tag_counts(senders: list[dict]) -> list[dict[str, Any]]:
    """Multi-select chip data: each unique tag with sender count, sorted by name."""
    counts: Counter[str] = Counter()
    for row in senders:
        for tag in row.get("tags") or []:
            if isinstance(tag, str) and tag:
                counts[tag] += 1
    return [{"tag": tag, "count": counts[tag]} for tag in sorted(counts.keys())]


def build_bundles(
    senders: list[dict],
    *,
    limit_key: str = "daily_limit",
) -> list[dict[str, Any]]:
    """Group senders by daily limit with Set 1–6 heatmap counts.

    A sender with multiple set tags contributes to each matching column.
    Untagged (no Set 1–6) senders still count toward the row total.
    """
    by_limit: dict[int, list[dict]] = defaultdict(list)
    for row in senders:
        limit = int(row.get(limit_key) if row.get(limit_key) is not None else row.get("daily_limit") or 0)
        by_limit[limit].append(row)

    bundles: list[dict[str, Any]] = []
    for limit in sorted(by_limit.keys()):
        rows = by_limit[limit]
        set_counts = {f"set_{n}": 0 for n in SET_COLUMNS}
        for row in rows:
            for n in row.get("sets") or extract_set_indexes(row.get("tags") or []):
                if n in SET_COLUMNS:
                    set_counts[f"set_{n}"] += 1
        bundles.append(
            {
                "daily_limit": limit,
                "count": len(rows),
                "sender_email_ids": [str(r["sender_email_id"]) for r in rows],
                **set_counts,
            }
        )
    return bundles


def normalize_sender_row(row: dict, *, sent_today: Optional[int] = None) -> dict:
    """Shape a sender_daily_stats (+ tags) row for the daily-limits payload."""
    tags = normalize_tags(row.get("tags"))
    sets = extract_set_indexes(tags)
    limit = int(row.get("daily_limit") or 0)
    sid = row.get("sender_email_id")
    return {
        "sender_email_id": str(sid) if sid is not None else "",
        "workspace_id": row.get("workspace_id"),
        "sender_email": row.get("sender_email") or "",
        "domain": row.get("domain") or "",
        "daily_limit": limit,
        "sent_today": sent_today,
        "tags": tags,
        "sets": sets,
    }


def preview_change(
    selected: list[dict],
    mode: str,
    value: int,
) -> dict[str, Any]:
    """Preview capacity impact for Set to / Increase by / Decrease by."""
    if value < 0:
        raise ValueError("value must be >= 0")
    if mode not in ("set", "increase", "decrease"):
        raise ValueError("mode must be set, increase, or decrease")

    capacity_now = 0
    capacity_after = 0
    target_by_limit: Counter[int] = Counter()
    updates: list[dict[str, Any]] = []

    for row in selected:
        old = int(row.get("daily_limit") or 0)
        if mode == "set":
            new = value
        elif mode == "increase":
            new = old + value
        else:
            new = max(0, old - value)
        capacity_now += old
        capacity_after += new
        target_by_limit[new] += 1
        updates.append(
            {
                "sender_email_id": str(row["sender_email_id"]),
                "workspace_id": row.get("workspace_id"),
                "from": old,
                "to": new,
            }
        )

    return {
        "selected_count": len(selected),
        "capacity_now": capacity_now,
        "capacity_after": capacity_after,
        "capacity_change": capacity_after - capacity_now,
        "target_by_limit": [
            {"daily_limit": lim, "count": target_by_limit[lim]}
            for lim in sorted(target_by_limit.keys())
        ],
        "updates": updates,
    }
