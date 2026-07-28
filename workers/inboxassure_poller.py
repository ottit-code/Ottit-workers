"""
inboxassure_poller.py — runs hourly (and on manual data refresh)

Pulls the latest InboxAssure placement test results into Supabase
(inboxassure_placement_results). Strictly read-only: it never launches
tests on the InboxAssure side, it only stores results that already exist.

Dormant until INBOXASSURE_API_TOKEN is configured — run() is then a no-op.

NOTE: InboxAssure has no public API docs yet; the field mapping below is
defensive and keeps the full payload in `raw` so nothing is lost while the
exact schema is confirmed.
"""

import logging
from typing import Any, Optional

from lib import inboxassure
from lib.supabase_client import get_supabase

logger = logging.getLogger(__name__)


def _first(item: dict, *fields: str) -> Any:
    for f in fields:
        val = item.get(f)
        if val is not None:
            return val
    return None


def _num(val: Any) -> Optional[float]:
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _normalize(item: dict) -> Optional[dict]:
    test_id = _first(item, "uuid", "id", "test_id")
    if test_id is None:
        return None
    providers = item.get("providers") or item.get("provider_scores") or {}
    return {
        "ia_test_id": str(test_id),
        "domain": _first(item, "domain", "sending_domain"),
        "inbox_email": _first(item, "inbox_email", "sender_email", "email", "from_email"),
        "status": _first(item, "status", "state"),
        "overall_score": _num(_first(item, "overall_score", "score", "placement_score")),
        "google_score": _num(
            _first(item, "google_score")
            or (providers.get("google") if isinstance(providers, dict) else None)
        ),
        "outlook_score": _num(
            _first(item, "outlook_score")
            or (providers.get("outlook") if isinstance(providers, dict) else None)
        ),
        "inbox_count": item.get("inbox_count"),
        "spam_count": item.get("spam_count"),
        "missing_count": item.get("missing_count"),
        "test_completed_at": _first(item, "completed_at", "finished_at"),
        "test_created_at": item.get("created_at"),
        "raw": item,
    }


def run() -> None:
    """Main entry point called by the scheduler and the manual refresh."""
    if not inboxassure.is_configured():
        logger.info("inboxassure_poller skipped — INBOXASSURE_API_TOKEN not set")
        return

    logger.info("Starting InboxAssure poll")
    try:
        results = inboxassure.get_placement_results()
    except Exception as e:
        logger.error(f"InboxAssure results fetch failed: {e}")
        raise

    rows = [r for r in (_normalize(item) for item in results) if r is not None]
    if not rows:
        logger.info("No InboxAssure results to store")
        return

    try:
        get_supabase().table("inboxassure_placement_results").upsert(
            rows, on_conflict="ia_test_id"
        ).execute()
        logger.info(f"Batch-upserted {len(rows)} InboxAssure placement results")
    except Exception as e:
        logger.error(f"Failed to upsert InboxAssure results: {e}")
        raise


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
