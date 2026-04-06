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
    """Upsert inbox placement tests; for completed tests, write per-email child rows."""
    supabase = get_supabase()
    tests = emailguard.get_inbox_placement_tests()
    logger.info(f"Polling {len(tests)} placement tests")

    for test in tests:
        uuid = test.get("uuid") or test.get("id", "")
        if not uuid:
            continue

        row = {
            "eg_test_uuid": str(uuid),
            "domain": test.get("domain") or "",
            "sender_email_id": int(test.get("sender_email_id", 0)),
            "sender_email": test.get("sender_email") or "",
            "triggered_by": "delivery_poller",
            "status": test.get("status") or "pending",
            "created_at": test.get("created_at"),
            "completed_at": test.get("completed_at"),
            "overall_score": test.get("overall_score"),
            "passed": test.get("passed"),
        }

        try:
            supabase.table("domain_placement_tests").upsert(
                row, on_conflict="eg_test_uuid"
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
                    "eg_test_uuid", str(uuid)
                ).single().execute()
                test_id = existing.data["id"] if existing.data else None

                if test_id and emails:
                    for email_item in emails:
                        eg_email_uuid = str(
                            email_item.get("uuid") or email_item.get("id", "")
                        )
                        if not eg_email_uuid:
                            logger.warning(
                                f"placement_test_email missing uuid for test {uuid}, skipping"
                            )
                            continue
                        child = {
                            "placement_test_id": test_id,
                            "eg_email_uuid": eg_email_uuid,
                            "email": email_item.get("email") or "",
                            "provider": email_item.get("provider"),
                            "status": email_item.get("status"),
                            # folder is "inbox", "spam", "promotions" as a string
                            "folder": email_item.get("folder"),
                        }
                        supabase.table("placement_test_emails").upsert(
                            child, on_conflict="eg_email_uuid"
                        ).execute()

                    logger.debug(f"Wrote {len(emails)} email rows for test {uuid}")

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
        }

        try:
            supabase.table("spam_filter_tests").upsert(
                row, on_conflict="eg_test_uuid"
            ).execute()
        except Exception as e:
            logger.error(f"Failed to upsert spam filter test {uuid}: {e}")


def poll_surbl_checks() -> None:
    """Upsert SURBL blacklist check results into surbl_checks."""
    supabase = get_supabase()
    checks = emailguard.get_surbl_checks()
    logger.info(f"Polling {len(checks)} SURBL checks")

    for check in checks:
        domain = check.get("domain", "")
        if not domain:
            continue

        eg_uuid = check.get("uuid") or check.get("id", "")
        if not eg_uuid:
            logger.warning(f"SURBL check missing uuid, skipping: {check}")
            continue

        row = {
            "eg_check_uuid": str(eg_uuid),
            "domain": domain,
            "status": check.get("status"),
            "listed": bool(check.get("listed", False)),
            "triggered_by": "delivery_poller",
            "created_at": check.get("created_at"),
            "completed_at": check.get("completed_at"),
        }

        try:
            supabase.table("surbl_checks").upsert(
                row, on_conflict="eg_check_uuid"
            ).execute()
        except Exception as e:
            logger.error(f"Failed to upsert SURBL check for {domain}: {e}")


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
