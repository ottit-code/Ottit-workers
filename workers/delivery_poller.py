"""
delivery_poller.py — runs every 2 hours

Pulls EmailGuard deliverability data into Supabase:
- GET /api/v1/inbox-placement-tests → domain_placement_tests + placement_test_emails
- GET /api/v1/spam-filter-tests → spam_filter_tests
- GET /api/v1/surbl-blacklist-checks/domains → surbl_checks
"""

import logging
from lib import emailguard
from lib.supabase_client import get_supabase

logger = logging.getLogger(__name__)


def poll_placement_tests() -> None:
    """Upsert inbox placement tests; for completed tests, write per-provider child rows."""
    supabase = get_supabase()
    tests = emailguard.get_inbox_placement_tests()
    logger.info(f"Polling {len(tests)} placement tests")

    for test in tests:
        uuid = test.get("uuid") or test.get("id", "")
        if not uuid:
            continue

        row = {
            "external_uuid": str(uuid),
            "domain": test.get("domain"),
            "status": test.get("status"),
            "created_at": test.get("created_at"),
            "completed_at": test.get("completed_at"),
        }

        try:
            result = supabase.table("domain_placement_tests").upsert(
                row, on_conflict="external_uuid"
            ).execute()
        except Exception as e:
            logger.error(f"Failed to upsert placement test {uuid}: {e}")
            continue

        # If completed, fetch full test and write child rows
        if test.get("status") == "completed":
            try:
                full_test = emailguard.get_inbox_placement_test(str(uuid))
                emails = full_test.get("inbox_placement_test_emails") or []

                # Get the internal Supabase id
                existing = supabase.table("domain_placement_tests").select("id").eq(
                    "external_uuid", str(uuid)
                ).single().execute()
                test_id = existing.data["id"] if existing.data else None

                if test_id and emails:
                    # Delete existing child rows to avoid duplicates
                    supabase.table("placement_test_emails").delete().eq(
                        "test_id", test_id
                    ).execute()

                    child_rows = [
                        {
                            "test_id": test_id,
                            "provider": email.get("provider"),
                            "inbox": email.get("inbox"),
                            "spam": email.get("spam"),
                            "promotions": email.get("promotions"),
                            "raw": email,
                        }
                        for email in emails
                    ]
                    supabase.table("placement_test_emails").insert(child_rows).execute()
                    logger.debug(f"Wrote {len(child_rows)} email rows for test {uuid}")

            except Exception as e:
                logger.error(f"Failed to fetch/write placement test emails for {uuid}: {e}")


def poll_spam_filter_tests() -> None:
    """Upsert spam filter tests into spam_filter_tests."""
    supabase = get_supabase()
    tests = emailguard.get_spam_filter_tests()
    logger.info(f"Polling {len(tests)} spam filter tests")

    for test in tests:
        uuid = test.get("uuid") or test.get("id", "")
        if not uuid:
            continue

        row = {
            "external_uuid": str(uuid),
            "domain": test.get("domain"),
            "score": test.get("score"),
            "status": test.get("status"),
            "raw": test,
            "created_at": test.get("created_at"),
        }

        try:
            supabase.table("spam_filter_tests").upsert(
                row, on_conflict="external_uuid"
            ).execute()
        except Exception as e:
            logger.error(f"Failed to upsert spam filter test {uuid}: {e}")


def poll_surbl_checks() -> None:
    """Insert SURBL blacklist check results into surbl_checks."""
    supabase = get_supabase()
    checks = emailguard.get_surbl_checks()
    logger.info(f"Polling {len(checks)} SURBL checks")

    for check in checks:
        domain = check.get("domain", "")
        if not domain:
            continue

        row = {
            "domain": domain,
            "listed": bool(check.get("listed", False)),
            "details": check,
            "checked_at": check.get("checked_at"),
        }

        try:
            supabase.table("surbl_checks").insert(row).execute()
        except Exception as e:
            logger.error(f"Failed to insert SURBL check for {domain}: {e}")


def run() -> None:
    """Main entry point called by the scheduler."""
    logger.info("Starting delivery poll")
    for fn in [poll_placement_tests, poll_spam_filter_tests, poll_surbl_checks]:
        try:
            fn()
        except Exception as e:
            logger.error(f"delivery_poller.{fn.__name__} failed: {e}")
    logger.info("Delivery poll complete")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
