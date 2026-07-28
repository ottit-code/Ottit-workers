"""
delivery_poller.py — runs every 2 hours

Pulls EmailGuard deliverability data into Supabase:
- GET /api/v1/inbox-placement-tests → domain_placement_tests + placement_test_emails
- GET /api/v1/spam-filter-tests → spam_filter_tests
- GET /api/v1/surbl-blacklist-checks/domains → surbl_checks
"""

import logging
from lib import emailguard
from lib.config import eg_pollable_workspaces
from lib.supabase_client import get_supabase

logger = logging.getLogger(__name__)


def _domain_from_email(email: str) -> str:
    """Extract domain from an email address, e.g. 'user@example.com' → 'example.com'."""
    if email and "@" in email:
        return email.split("@", 1)[1].lower()
    return ""


def poll_placement_tests(workspace_id: str = "ws_v1", guard=None) -> None:
    """Batch-upsert inbox placement tests; for completed tests, batch-write per-email child rows."""
    supabase = get_supabase()
    guard = guard or emailguard
    tests = guard.get_inbox_placement_tests()
    logger.info(f"[{workspace_id}] Polling {len(tests)} placement tests")

    parent_rows = []
    for test in tests:
        uuid = test.get("uuid") or test.get("id", "")
        if not uuid:
            continue
        parent_rows.append({
            "workspace_id": workspace_id,
            "eg_test_uuid": str(uuid),
            "domain": test.get("domain") or _domain_from_email(test.get("sender_email") or ""),
            "sender_email_id": int(test.get("sender_email_id", 0)),
            "sender_email": test.get("sender_email") or "",
            "triggered_by": "delivery_poller",
            "status": test.get("status") or "pending",
            "created_at": test.get("created_at"),
            "completed_at": test.get("completed_at"),
            "overall_score": test.get("overall_score"),
            "passed": test.get("passed"),
        })

    # For rows still missing domain/sender_email, look up from sender_daily_stats
    missing_ids = [r["sender_email_id"] for r in parent_rows if not r["domain"] and r["sender_email_id"]]
    if missing_ids:
        try:
            sender_rows = (
                supabase.table("sender_daily_stats")
                .select("sender_email_id,sender_email,domain")
                .in_("sender_email_id", list(set(missing_ids)))
                .execute()
                .data
            )
            sender_map = {
                s["sender_email_id"]: (s.get("domain") or _domain_from_email(s.get("sender_email") or ""), s.get("sender_email") or "")
                for s in sender_rows
            }
            for row in parent_rows:
                if not row["domain"] and row["sender_email_id"] in sender_map:
                    row["domain"], row["sender_email"] = sender_map[row["sender_email_id"]]
        except Exception as e:
            logger.warning(f"Could not look up sender domains: {e}")

    if not parent_rows:
        return

    try:
        result = supabase.table("domain_placement_tests").upsert(
            parent_rows, on_conflict="eg_test_uuid", returning="representation"
        ).execute()
        # Build a map of eg_test_uuid → internal id from the returned rows
        uuid_to_id = {row["eg_test_uuid"]: row["id"] for row in (result.data or [])}
        logger.info(f"Batch-upserted {len(parent_rows)} placement tests")
    except Exception as e:
        logger.error(f"Failed to batch-upsert placement tests: {e}")
        return

    # For completed tests, fetch full details and collect all child email rows
    child_rows = []
    for test in tests:
        uuid = str(test.get("uuid") or test.get("id", ""))
        if test.get("status") != "completed" or not uuid:
            continue

        test_id = uuid_to_id.get(uuid)
        if not test_id:
            continue

        try:
            full_test = guard.get_inbox_placement_test(uuid)
            emails = full_test.get("inbox_placement_test_emails") or []
            for email_item in emails:
                eg_email_uuid = str(email_item.get("uuid") or email_item.get("id", ""))
                if not eg_email_uuid:
                    logger.warning(f"placement_test_email missing uuid for test {uuid}, skipping")
                    continue
                child_rows.append({
                    "placement_test_id": test_id,
                    "eg_email_uuid": eg_email_uuid,
                    "email": email_item.get("email") or "",
                    "provider": email_item.get("provider"),
                    "status": email_item.get("status"),
                    "folder": email_item.get("folder"),
                })
        except Exception as e:
            logger.error(f"Failed to fetch placement test emails for {uuid}: {e}")

    if child_rows:
        try:
            supabase.table("placement_test_emails").upsert(
                child_rows, on_conflict="eg_email_uuid"
            ).execute()
            logger.info(f"Batch-upserted {len(child_rows)} placement test email rows")
        except Exception as e:
            logger.error(f"Failed to batch-upsert placement test emails: {e}")


def poll_spam_filter_tests(workspace_id: str = "ws_v1", guard=None) -> None:
    """Batch-upsert spam filter tests into spam_filter_tests."""
    supabase = get_supabase()
    guard = guard or emailguard
    tests = guard.get_spam_filter_tests()
    logger.info(f"[{workspace_id}] Polling {len(tests)} spam filter tests")

    rows = []
    for test in tests:
        uuid = test.get("uuid") or test.get("id", "")
        if not uuid:
            continue
        rows.append({
            "eg_test_uuid": str(uuid),
            "sender_email_id": int(test.get("sender_email_id", 0)) if test.get("sender_email_id") else None,
            "sender_email": test.get("sender_email"),
            "domain": test.get("domain"),
            "status": test.get("status"),
            "score": test.get("score"),
            "score_breakdown": test.get("score_breakdown") or test,
            "sent_from": test.get("sent_from"),
            "sending_server_ip": test.get("sending_server_ip"),
            "triggered_by": "delivery_poller",
            "created_at": test.get("created_at"),
            "completed_at": test.get("completed_at"),
        })

    if rows:
        try:
            supabase.table("spam_filter_tests").upsert(
                rows, on_conflict="eg_test_uuid"
            ).execute()
            logger.info(f"Batch-upserted {len(rows)} spam filter tests")
        except Exception as e:
            logger.error(f"Failed to batch-upsert spam filter tests: {e}")


def poll_surbl_checks(workspace_id: str = "ws_v1", guard=None) -> None:
    """Batch-upsert SURBL blacklist check results into surbl_checks."""
    supabase = get_supabase()
    guard = guard or emailguard
    checks = guard.get_surbl_checks()
    logger.info(f"[{workspace_id}] Polling {len(checks)} SURBL checks")

    rows = []
    for check in checks:
        domain = check.get("domain", "")
        if not domain:
            continue
        eg_uuid = check.get("uuid") or check.get("id", "")
        if not eg_uuid:
            logger.warning(f"SURBL check missing uuid, skipping: {check}")
            continue
        rows.append({
            "eg_check_uuid": str(eg_uuid),
            "domain": domain,
            "status": check.get("status"),
            "listed": bool(check.get("listed", False)),
            "triggered_by": "delivery_poller",
            "created_at": check.get("created_at"),
            "completed_at": check.get("completed_at"),
        })

    if rows:
        try:
            supabase.table("surbl_checks").upsert(
                rows, on_conflict="eg_check_uuid"
            ).execute()
            logger.info(f"Batch-upserted {len(rows)} SURBL checks")
        except Exception as e:
            logger.error(f"Failed to batch-upsert SURBL checks: {e}")


def run() -> None:
    """Main entry point called by the scheduler."""
    logger.info("Starting delivery poll")
    for ws in eg_pollable_workspaces():
        try:
            guard = emailguard.for_workspace(ws["id"])
        except Exception as e:
            logger.error(f"Skipping workspace {ws['id']}: {e}")
            continue
        for fn in [poll_placement_tests, poll_spam_filter_tests, poll_surbl_checks]:
            try:
                fn(ws["id"], guard)
            except Exception as e:
                logger.error(f"[{ws['id']}] delivery_poller.{fn.__name__} failed: {e}")
    logger.info("Delivery poll complete")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
