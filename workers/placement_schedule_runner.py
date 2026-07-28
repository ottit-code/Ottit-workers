"""
placement_schedule_runner.py — runs every 15 minutes

Triggers due recurring inbox-placement tests (placement_test_schedules rows
with enabled = true and next_run_at <= now) via EmailGuard, then advances
next_run_at by the schedule's cadence. Results are synced back into
domain_placement_tests by the existing delivery_poller.
"""

import logging
from datetime import datetime, timedelta, timezone

from lib import emailguard
from lib.supabase_client import get_supabase

logger = logging.getLogger(__name__)

_CADENCE_DELTAS = {
    "daily": timedelta(days=1),
    "weekly": timedelta(weeks=1),
    "monthly": timedelta(days=30),
}


def _advance(next_run_at: datetime, cadence: str, now: datetime) -> datetime:
    """Advance next_run_at by the cadence until it is in the future.

    Catch-up loop so a schedule that was down for a while fires once,
    not once per missed interval.
    """
    delta = _CADENCE_DELTAS.get(cadence, _CADENCE_DELTAS["weekly"])
    advanced = next_run_at
    while advanced <= now:
        advanced += delta
    return advanced


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def run_due_schedules() -> None:
    supabase = get_supabase()
    now = datetime.now(timezone.utc)

    try:
        due = (
            supabase.table("placement_test_schedules")
            .select("*")
            .eq("enabled", True)
            .lte("next_run_at", now.isoformat())
            .execute()
            .data or []
        )
    except Exception as e:
        logger.error(f"Failed to fetch due placement test schedules: {e}")
        return

    if not due:
        return
    logger.info(f"Triggering {len(due)} due placement test schedule(s)")

    for schedule in due:
        sched_id = schedule["id"]
        payload: dict = {}
        if schedule.get("domain"):
            payload["domain"] = schedule["domain"]
        if schedule.get("sender_email"):
            payload["sender_email"] = schedule["sender_email"]

        update: dict = {
            "last_run_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "next_run_at": _advance(
                _parse_ts(schedule["next_run_at"]), schedule.get("cadence", "weekly"), now
            ).isoformat(),
        }
        try:
            result = emailguard.post("/api/v1/inbox-placement-tests", payload)
            data = result.get("data") if isinstance(result, dict) else None
            test_uuid = (data or {}).get("uuid") if isinstance(data, dict) else None
            update["last_test_uuid"] = test_uuid
            update["last_error"] = None
            logger.info(
                f"Placement test triggered for schedule {sched_id} "
                f"({payload}), uuid={test_uuid}"
            )
        except Exception as e:
            update["last_error"] = str(e)[:500]
            logger.error(f"Placement test trigger failed for schedule {sched_id}: {e}")

        try:
            supabase.table("placement_test_schedules").update(update).eq(
                "id", sched_id
            ).execute()
        except Exception as e:
            logger.error(f"Failed to update schedule {sched_id}: {e}")


def run() -> None:
    """Main entry point called by the scheduler."""
    try:
        run_due_schedules()
    except Exception as e:
        logger.error(f"placement_schedule_runner.run_due_schedules failed: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
