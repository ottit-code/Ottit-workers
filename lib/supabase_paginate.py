"""Helpers for reading past Supabase's server-side row cap (1000 rows/request).

PostgREST silently truncates any select or RPC response at the project's
max-rows setting, so unbounded reads that "look fine" quietly drop data once a
table grows. Use fetch_all with a query-builder factory (a fresh builder per
page — builders are single-use).
"""
from __future__ import annotations

from typing import Callable

PAGE_SIZE = 1000


def fetch_all(build_query: Callable, page_size: int = PAGE_SIZE) -> list[dict]:
    """All rows for a query, paged past the PostgREST row cap.

    `build_query` must return a *fresh* filter builder each call (e.g.
    ``lambda: sb.table("x").select("*").eq("a", 1).order("id")``). Works for
    table selects and set-returning RPCs alike. Always order deterministically
    so pages don't overlap.
    """
    out: list[dict] = []
    offset = 0
    while True:
        rows = (
            build_query().range(offset, offset + page_size - 1).execute().data or []
        )
        out.extend(rows)
        if len(rows) < page_size:
            return out
        offset += page_size
