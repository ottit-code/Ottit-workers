"""
domain_blacklist_poller.py — runs every 12 hours

Pulls EmailGuard domain blacklist check results into Supabase:
- GET /api/v1/blacklist-checks/domains (all pages)
  → domain_blacklist_checks (upsert on eg_check_uuid)
"""

import logging
from datetime import datetime, timezone
from lib import emailguard
from lib.config import eg_pollable_workspaces
from lib.supabase_client import get_supabase

logger = logging.getLogger(__name__)


def poll_workspace(workspace_id: str, guard) -> None:
    supabase = get_supabase()

    try:
        checks = guard.get_domain_blacklist_checks()
    except Exception as e:
        logger.error(f"[{workspace_id}] Failed to fetch domain blacklist checks: {e}")
        return

    logger.info(f"[{workspace_id}] Fetched {len(checks)} domain blacklist checks")

    rows = []
    for check in checks:
        eg_uuid = check.get("uuid") or check.get("id", "")
        if not eg_uuid:
            logger.warning(f"Domain blacklist check missing uuid, skipping: {check}")
            continue
        rows.append({
            "eg_check_uuid": str(eg_uuid),
            "domain": check.get("domain", ""),
            "ip": check.get("ip"),
            "type": check.get("type"),
            "status": check.get("status"),
            "blacklists_count": int(check.get("blacklists_count") or 0),
            "blacklists": check.get("blacklists") or [],
            "last_polled_at": datetime.now(timezone.utc).isoformat(),
        })

    if not rows:
        logger.info("No domain blacklist rows to upsert")
        return

    try:
        supabase.table("domain_blacklist_checks").upsert(
            rows, on_conflict="eg_check_uuid"
        ).execute()
        blacklisted = sum(1 for r in rows if r["blacklists_count"] > 0)
        logger.info(
            f"Upserted {len(rows)} domain blacklist checks "
            f"({blacklisted} blacklisted, {len(rows) - blacklisted} clean)"
        )
    except Exception as e:
        logger.error(f"Failed to upsert domain blacklist checks: {e}")


def run() -> None:
    logger.info("Starting domain blacklist poll")
    for ws in eg_pollable_workspaces():
        try:
            guard = emailguard.for_workspace(ws["id"])
        except Exception as e:
            logger.error(f"Skipping workspace {ws['id']}: {e}")
            continue
        poll_workspace(ws["id"], guard)
    logger.info("Domain blacklist poll complete")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
