"""Zombie heatmap + trends payload — per-inbox day triples by tag set.

Mirrors bison-reports /zombie/data.json semantics:
- days = sending days (any inbox sent > 0)
- cells[].d = flat [dayIndex, sends, replies, ...] triples
- reply counts prefer cohort (sent-date) attribution

Pure builders live here so unit tests don't need Supabase.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Iterable, Optional

from lib.warmup_report import normalize_tags, normalize_workspace_id

logger = logging.getLogger(__name__)

# Rate bundles: first four are "under 2%"; bottom two bins (rb<=1) drive the watchlist.
RATE_BINS: list[list[Any]] = [
    [0, 1, "0–0.99%"],
    [1, 1.25, "1–1.24%"],
    [1.25, 1.75, "1.25–1.74%"],
    [1.75, 2, "1.75–1.99%"],
    [2, 3, "2–2.99%"],
    [3, 1_000_000_000, "3%+"],
]

COUNT_BINS: list[list[int]] = [
    [0, 3],
    [4, 5],
    [6, 7],
    [8, 9],
    [10, 11],
    [12, 14],
    [15, 1_000_000_000],
]

WATCH_MIN_SENDS = 40

_SET_RE = re.compile(r"(?i)(?:^|[-_\s])set\s*([1-6])(?=$|[-_\s])")
_FAMILY_RE = re.compile(r"(?i)^(.+?)[-_\s]set\s*[1-6](?:[-_\s].*)?$")

_DELTA_FIELDS = ("emails_sent", "emails_bounced")


def rate_bin(rt: float) -> int:
    for i, (lo, hi, *_rest) in enumerate(RATE_BINS):
        if rt >= lo and rt < hi:
            return i
    return len(RATE_BINS) - 1


def count_bin(replies: int) -> int:
    for i, (lo, hi) in enumerate(COUNT_BINS):
        if lo <= replies <= hi:
            return i
    return len(COUNT_BINS) - 1


def reply_rate(sent: int, replies: int) -> float:
    if sent <= 0:
        return 0.0
    return round(100.0 * replies / sent, 2)


def pick_set_tag(tags: Iterable[str]) -> Optional[str]:
    """Prefer a Set 1–6 tag; otherwise the first cleaned tag."""
    cleaned = [t for t in tags if isinstance(t, str) and t]
    if not cleaned:
        return None
    for tag in cleaned:
        if _SET_RE.search(tag):
            return tag
    return cleaned[0]


def canonicalize_set(tag: str) -> tuple[str, str]:
    """Return (display_name, family) for a Bison set tag.

    SM-GOOG-0609-Set1 → (SM-GOOG-Set1, SM-GOOG)
    CI-DED-Set4-0518 → (CI-DED-Set4, CI-DED)
    """
    m = _FAMILY_RE.match(tag.strip())
    if not m:
        return tag, tag
    family_raw = m.group(1)
    # Drop trailing MMDD / YYYY date segments commonly glued onto family prefixes.
    family = re.sub(r"[-_]\d{3,8}$", "", family_raw)
    set_m = _SET_RE.search(tag)
    set_n = set_m.group(1) if set_m else "?"
    name = f"{family}-Set{set_n}"
    return name, family


def flat_day_triples(
    days: list[str], per_day: dict[str, tuple[int, int]]
) -> list[int]:
    """Encode sparse per-day (sent, replies) as flat [idx, sent, replies, ...]."""
    out: list[int] = []
    for i, day in enumerate(days):
        sent, replies = per_day.get(day, (0, 0))
        if sent or replies:
            out.extend([i, int(sent), int(replies)])
    return out


def stats_in_window(
    triples: list[int], lo: int, hi: int
) -> dict[str, Any]:
    """Sum flat triples whose day index falls in [lo, hi] inclusive."""
    sent = 0
    replies = 0
    for i in range(0, len(triples), 3):
        idx = triples[i]
        if lo <= idx <= hi:
            sent += triples[i + 1]
            replies += triples[i + 2]
    rt = reply_rate(sent, replies)
    return {
        "s": sent,
        "r": replies,
        "rt": rt,
        "rb": rate_bin(rt) if sent else 0,
        "cb": count_bin(replies),
    }


def flagged_by(stats: dict[str, Any], mode: str) -> bool:
    """Watchlist inclusion rule (bison-reports parity)."""
    if (stats.get("s") or 0) <= 0:
        return False
    if mode == "all":
        return (stats.get("rb") or 0) <= 1 or (stats.get("r") or 0) < 4
    return (stats.get("s") or 0) >= WATCH_MIN_SENDS and (stats.get("rb") or 0) <= 1


def _dist_for_cells(cells: list[dict[str, Any]], lo: int, hi: int) -> list[int]:
    dist = [0] * len(RATE_BINS)
    for cell in cells:
        st = stats_in_window(cell.get("d") or [], lo, hi)
        if st["s"] > 0:
            dist[st["rb"]] += 1
    return dist


def _set_rollup(cells: list[dict[str, Any]], lo: int, hi: int) -> dict[str, Any]:
    sent = 0
    replies = 0
    rates: list[float] = []
    live = 0
    for cell in cells:
        st = stats_in_window(cell.get("d") or [], lo, hi)
        sent += st["s"]
        replies += st["r"]
        if st["s"] > 0:
            live += 1
            rates.append(st["rt"])
    dist = _dist_for_cells(cells, lo, hi)
    under2 = sum(dist[:4])
    return {
        "n": len(cells),
        "live": live,
        "sent": sent,
        "rep": replies,
        "rate": reply_rate(sent, replies),
        "dist": dist,
        "under2": under2,
        "medRate": round(median(rates), 2) if rates else None,
        "minRate": round(min(rates), 2) if rates else None,
        "maxRate": round(max(rates), 2) if rates else None,
    }


def build_watchlist(
    sets: list[dict[str, Any]],
    days: list[str],
    basis: str = "7",
) -> list[dict[str, Any]]:
    """Multi-basis zombie watchlist rows for the given inclusion basis."""
    if not days:
        return []
    last = len(days) - 1
    bases = ("7", "14", "30", "all")

    def bounds(mode: str) -> tuple[int, int]:
        if mode == "all":
            return 0, last
        return max(0, last - int(mode) + 1), last

    # email → row skeleton + per-basis stats
    by_email: dict[str, dict[str, Any]] = {}
    for st in sets:
        for cell in st.get("cells") or []:
            email = cell.get("e") or cell.get("email")
            if not email:
                continue
            by_email[email] = {
                "set": st.get("name"),
                "e": email,
                "c": cell.get("c") or cell.get("contacted") or 0,
                "b": cell.get("b") or cell.get("bounced") or 0,
                "dl": cell.get("dl") or cell.get("daily_limit") or 0,
                "d": cell.get("d") or [],
            }

    rows: list[dict[str, Any]] = []
    for email, base in by_email.items():
        per_basis: dict[str, dict[str, Any]] = {}
        for mode in bases:
            lo, hi = bounds(mode)
            per_basis[mode] = stats_in_window(base["d"], lo, hi)
        if not flagged_by(per_basis[basis], basis):
            continue
        hits = sum(1 for mode in bases if flagged_by(per_basis[mode], mode))
        row = {
            "set": base["set"],
            "e": email,
            "c": base["c"],
            "b": base["b"],
            "dl": base["dl"],
            "hits": hits,
            "s": per_basis[basis]["s"],
            "r": per_basis[basis]["r"],
            "rt": per_basis[basis]["rt"],
            "rb": per_basis[basis]["rb"],
            "cb": per_basis[basis]["cb"],
        }
        for mode in bases:
            st = per_basis[mode]
            row[f"s_{mode}"] = st["s"]
            row[f"r_{mode}"] = st["r"]
            row[f"rt_{mode}"] = st["rt"] if st["s"] > 0 else None
        rows.append(row)
    rows.sort(key=lambda r: (r["rt"], -r["s"], r["e"]))
    return rows


def build_zombie_payload(
    *,
    days: list[str],
    inbox_rows: list[dict[str, Any]],
    generated_at: Optional[str] = None,
) -> dict[str, Any]:
    """Assemble the zombie/trends payload from prepared inbox series.

    Each inbox_row:
      email, tags (list[str]), daily_limit, contacted, bounced,
      daily: {YYYY-MM-DD: {"sent": int, "replies": int}}
    """
    day_list = sorted(days)
    by_set: dict[str, dict[str, Any]] = {}

    for row in inbox_rows:
        email = (row.get("email") or "").lower()
        if not email:
            continue
        tag = pick_set_tag(normalize_tags(row.get("tags")))
        if not tag:
            name, family = "Untagged", "Untagged"
        else:
            name, family = canonicalize_set(tag)
        daily = row.get("daily") or {}
        per_day = {
            d: (int(v.get("sent") or 0), int(v.get("replies") or 0))
            for d, v in daily.items()
        }
        cell = {
            "e": email,
            "sid": row.get("sender_email_id"),
            "dl": int(row.get("daily_limit") or 0),
            "c": int(row.get("contacted") or 0),
            "b": int(row.get("bounced") or 0),
            "d": flat_day_triples(day_list, per_day),
        }
        bucket = by_set.setdefault(
            name, {"name": name, "family": family, "cells": []}
        )
        bucket["cells"].append(cell)

    last = max(0, len(day_list) - 1)
    sets_out: list[dict[str, Any]] = []
    for name in sorted(by_set.keys()):
        bucket = by_set[name]
        cells = sorted(bucket["cells"], key=lambda c: c["e"])
        rollup = _set_rollup(cells, 0, last)
        sets_out.append(
            {
                "name": name,
                "family": bucket["family"],
                "n": rollup["n"],
                "sent": rollup["sent"],
                "rep": rollup["rep"],
                "rate": rollup["rate"],
                "cells": cells,
                "perInbox": round(rollup["sent"] / rollup["n"], 1) if rollup["n"] else 0,
                "contacted": sum(c["c"] for c in cells),
                "bounced": sum(c["b"] for c in cells),
                "medRate": rollup["medRate"],
                "minRate": rollup["minRate"],
                "maxRate": rollup["maxRate"],
                "dist": rollup["dist"],
            }
        )

    all_cells = [c for s in sets_out for c in s["cells"]]
    totals = _set_rollup(all_cells, 0, last)
    watch = build_watchlist(sets_out, day_list, basis="7")

    return {
        "days": day_list,
        "rateBins": RATE_BINS,
        "countBins": COUNT_BINS,
        "sets": sets_out,
        "totals": {
            "n": totals["n"],
            "sent": totals["sent"],
            "rep": totals["rep"],
            "rate": totals["rate"],
            "dist": totals["dist"],
            "under2": totals["under2"],
            "live": totals["live"],
        },
        "watch": watch,
        "meta": {
            "generatedAt": generated_at
            or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source": "sender_daily_stats+cohort_replies",
            "watchMinSends": WATCH_MIN_SENDS,
            "dayCount": len(day_list),
        },
    }


def _fetch_perf_map(ws_filter: Optional[str]) -> dict[int, dict]:
    """Latest tags + leads contacted per sender_email_id."""
    from lib.supabase_client import get_supabase
    from lib.supabase_paginate import fetch_all

    since = (datetime.now(timezone.utc).date() - timedelta(days=14)).isoformat()
    try:
        def build():
            q = (
                get_supabase()
                .table("sender_email_performance")
                .select(
                    "sender_email_id,tags,total_leads_contacted_count,snapshot_date,workspace_id"
                )
                .gte("snapshot_date", since)
                .order("snapshot_date", desc=True)
                .order("sender_email_id")
            )
            if ws_filter:
                q = q.eq("workspace_id", ws_filter)
            return q

        rows = fetch_all(build)
    except Exception as e:
        logger.warning(f"zombie perf lookup failed: {e}")
        return {}

    out: dict[int, dict] = {}
    for r in rows:
        sid = r.get("sender_email_id")
        if sid is None or int(sid) in out:
            continue
        out[int(sid)] = {
            "tags": normalize_tags(r.get("tags")),
            "contacted": int(r.get("total_leads_contacted_count") or 0),
        }
    return out


def _fetch_stats_rows(
    range_start: str, range_end: str, ws_filter: Optional[str]
) -> list[dict]:
    """Snapshots from the day before range_start through range_end (for deltas)."""
    from lib.supabase_client import get_supabase
    from lib.supabase_paginate import fetch_all

    # One day of lookback so the first in-range day can delta against a base.
    try:
        start_dt = datetime.fromisoformat(range_start).date() - timedelta(days=1)
    except ValueError:
        start_dt = datetime.now(timezone.utc).date() - timedelta(days=91)
    lookback = start_dt.isoformat()

    cols = (
        "workspace_id,sender_email_id,sender_email,domain,stat_date,"
        "emails_sent,emails_replied,emails_bounced,daily_limit"
    )

    def build():
        q = (
            get_supabase()
            .table("sender_daily_stats")
            .select(cols)
            .gte("stat_date", lookback)
            .lte("stat_date", range_end)
            .order("sender_email_id")
            .order("stat_date")
        )
        if ws_filter:
            q = q.eq("workspace_id", ws_filter)
        return q

    return fetch_all(build)


def _delta_series(
    history: list[dict], range_start: str, range_end: str
) -> tuple[dict[str, dict[str, int]], dict[str, Any]]:
    """Return (per_day sent/bounced, latest meta) for one sender history."""
    per_day: dict[str, dict[str, int]] = {}
    prev: Optional[dict] = None
    latest_in_range: Optional[dict] = None
    for r in history:
        day = str(r.get("stat_date") or "")[:10]
        if not day:
            continue
        if prev is not None and range_start <= day <= range_end:
            sent = max(0, (r.get("emails_sent") or 0) - (prev.get("emails_sent") or 0))
            bounced = max(
                0, (r.get("emails_bounced") or 0) - (prev.get("emails_bounced") or 0)
            )
            per_day[day] = {"sent": sent, "bounced": bounced, "replies": 0}
            latest_in_range = r
        prev = r
    meta = latest_in_range or (history[-1] if history else {})
    return per_day, meta


def _fetch_cohort_map(
    range_start: str, range_end: str, ws_filter: Optional[str]
) -> dict[tuple, int]:
    """(sender_email, stat_date) → cohort replies via get_cohort_reply_counts."""
    from lib.supabase_client import get_supabase
    from lib.supabase_paginate import fetch_all

    params: dict[str, Any] = {
        "p_start": range_start,
        "p_end": range_end,
        "p_group": "sender",
    }
    if ws_filter:
        params["p_workspace_id"] = ws_filter
    rows = fetch_all(lambda: get_supabase().rpc("get_cohort_reply_counts", params))
    return {
        (str(r["group_key"]).lower(), str(r["stat_date"])): int(r["cohort_replies"] or 0)
        for r in rows
    }


def get_zombie_report(
    *,
    days: int = 90,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> dict[str, Any]:
    """Load Supabase rows and build the zombie/trends payload."""
    ws_filter = normalize_workspace_id(workspace_id)
    today = datetime.now(timezone.utc).date()
    try:
        range_end = (
            datetime.fromisoformat(end_date).date() if end_date else today
        )
        range_start = (
            datetime.fromisoformat(start_date).date()
            if start_date
            else range_end - timedelta(days=max(1, days) - 1)
        )
    except ValueError as e:
        raise ValueError("Dates must be YYYY-MM-DD") from e
    if range_start > range_end:
        raise ValueError("start_date must be <= end_date")

    rs, re_ = range_start.isoformat(), range_end.isoformat()
    rows = _fetch_stats_rows(rs, re_, ws_filter)
    perf = _fetch_perf_map(ws_filter)

    by_sender: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        key = (r.get("workspace_id"), r.get("sender_email_id"))
        by_sender[key].append(r)

    cohort: dict[tuple, int] = {}
    try:
        cohort = _fetch_cohort_map(rs, re_, ws_filter)
    except Exception as e:
        logger.warning(f"zombie cohort replies failed: {e}")

    inbox_rows: list[dict[str, Any]] = []
    active_days: set[str] = set()

    for history in by_sender.values():
        history.sort(key=lambda r: str(r.get("stat_date") or ""))
        per_day, meta = _delta_series(history, rs, re_)
        email = (meta.get("sender_email") or "").lower()
        if not email:
            continue
        daily: dict[str, dict[str, int]] = {}
        lifetime_bounce = int(meta.get("emails_bounced") or 0)
        for day, vals in per_day.items():
            replies = int(cohort.get((email, day), 0))
            sent = int(vals.get("sent") or 0)
            if sent or replies:
                daily[day] = {"sent": sent, "replies": replies}
                if sent:
                    active_days.add(day)
        sid = meta.get("sender_email_id")
        p = perf.get(int(sid), {}) if sid is not None else {}
        inbox_rows.append(
            {
                "email": email,
                "sender_email_id": sid,
                "tags": p.get("tags") or [],
                "daily_limit": int(meta.get("daily_limit") or 0),
                "contacted": int(p.get("contacted") or 0),
                "bounced": lifetime_bounce,
                "daily": daily,
            }
        )

    day_list = sorted(active_days)
    return build_zombie_payload(
        days=day_list,
        inbox_rows=inbox_rows,
        generated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )
