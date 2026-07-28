"""
utils.py — shared helpers for pollers.
"""

import logging
from lib import emailbison

logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = ["active", "queued", "paused"]


def get_active_campaign_ids(supabase) -> list[str]:
    """Return distinct campaign IDs for active/queued/paused campaigns.

    Queries the documents table (canonical RAG source) first. Falls back to
    the EmailBison campaigns API when the documents table has no results.
    """
    try:
        result = (
            supabase.table("documents")
            .select("metadata")
            .eq("metadata->>type", "campaign")
            .in_("metadata->>campaign_status", _ACTIVE_STATUSES)
            .execute()
        )
        ids: set[str] = set()
        for row in result.data or []:
            meta = row.get("metadata") or {}
            cid = meta.get("campaign_id")
            if cid:
                ids.add(str(cid))
        if ids:
            return list(ids)
        logger.warning(
            "No campaign IDs found in documents table; falling back to EmailBison API"
        )
    except Exception as e:
        logger.warning(f"Documents table query failed ({e}); falling back to EmailBison API")

    # Fallback: query EmailBison campaigns endpoint directly
    return get_active_campaign_ids_from_bison(emailbison)


def get_active_campaign_ids_from_bison(bison) -> list[str]:
    """Active campaign IDs straight from a Bison client (or the module-level
    default). Used for non-default workspaces, whose campaigns are not in the
    documents table."""
    try:
        campaigns = bison.get_campaigns()
        return [
            str(c.get("id"))
            for c in campaigns
            if str(c.get("status", "")).lower() in _ACTIVE_STATUSES and c.get("id")
        ]
    except Exception as e:
        logger.error(f"Failed to fetch active campaign IDs from EmailBison: {e}")
        return []
