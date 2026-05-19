"""Fetch the most recent lead_engagement_snapshot for prompt enrichment.

The query `WHERE lead_id = ? ORDER BY snapshot_date DESC LIMIT 1` is served
by the existing UNIQUE index `idx_lead_engage_lead_date (lead_id, snapshot_date)`
via Index Scan Backward (<1 ms in EXPLAIN even on 1.5M rows). No extra index
needed.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from lib.supabase_client import get_supabase

logger = logging.getLogger(__name__)

# Canonical column set for prompt enrichment. Keep this trimmed — anything
# selected here gets serialized into the user message, so adding columns
# directly increases token spend per draft.
_SNAPSHOT_COLS = "engagement_score, funnel_stage, campaign_engagements, tags, status"


def fetch_latest_snapshot(lead_id: int) -> Optional[Dict[str, Any]]:
    if not lead_id:
        return None
    try:
        resp = (
            get_supabase()
            .table("lead_engagement_snapshots")
            .select(_SNAPSHOT_COLS)
            .eq("lead_id", lead_id)
            .order("snapshot_date", desc=True)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        logger.debug("lead_enricher.snapshot_lookup_failed lead_id=%s err=%s", lead_id, exc)
        return None
    rows = resp.data or []
    return rows[0] if rows else None
