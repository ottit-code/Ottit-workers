"""Unit tests for /schedule/today merge helpers and sending-schedules mapping."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from api.routers.schedule import _merge_live_with_snapshot, _merge_workspaces
from lib.send_schedule import relative_schedule_day, plan_from_sending_schedules


def test_relative_schedule_day_maps_three_day_window():
    now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    assert relative_schedule_day("2026-08-01", now) == "today"
    assert relative_schedule_day("2026-08-02", now) == "tomorrow"
    assert relative_schedule_day("2026-08-03", now) == "day_after_tomorrow"
    assert relative_schedule_day("2026-07-31", now) is None
    assert relative_schedule_day("2026-08-04", now) is None


def test_plan_from_sending_schedules_maps_emails_being_sent():
    ws = {"id": "ws_v2", "name": "Ottit V2"}
    client = MagicMock()
    client.get_sending_schedules.return_value = [
        {
            "emails_being_sent": 5341,
            "campaign_id": 10,
            "campaign": {"id": 10, "name": "A"},
        },
        {
            "emails_being_sent": 0,
            "campaign_id": 11,
            "campaign": {"id": 11, "name": "Drained"},
        },
    ]
    with patch("lib.send_schedule.emailbison.for_workspace", return_value=client), patch(
        "lib.send_schedule.relative_schedule_day", return_value="today"
    ):
        rows = plan_from_sending_schedules(ws, "2026-08-01")

    assert rows is not None
    assert len(rows) == 1
    assert rows[0]["planned_today"] == 5341
    assert rows[0]["campaign_id"] == "10"
    assert rows[0]["source"] == "sending_schedules"
    client.get_sending_schedules.assert_called_once_with("today")


def test_merge_live_with_snapshot_uses_live_remaining():
    ws = {"id": "ws_v2", "name": "Ottit V2"}
    snap_rows = [
        {
            "campaign_id": "1",
            "campaign_name": "A",
            "planned": 1000,
            "captured_at": "2026-08-01T00:01:00+00:00",
            "inboxes": [{"email": "a@x.com", "planned": 1000}],
        },
        {
            "campaign_id": "2",
            "campaign_name": "B",
            "planned": 500,
            "captured_at": "2026-08-01T00:01:00+00:00",
            "inboxes": [],
        },
    ]
    # Campaign 1 still has future-queued mail; campaign 2 drained.
    # Live remaining excludes overdue (446), so 1000 - sent - overdue ≈ 554.
    live = [
        {
            "workspace_id": "ws_v2",
            "workspace_name": "Ottit V2",
            "campaign_id": "1",
            "campaign_name": "A",
            "planned_today": 554,
            "overdue_today": 446,
            "inboxes": [{"email": "a@x.com", "planned": 554}],
            "error": None,
        },
    ]

    campaigns, plan_total, snapshot_at = _merge_live_with_snapshot(ws, live, snap_rows)

    assert plan_total == 1500
    assert snapshot_at == "2026-08-01T00:01:00+00:00"
    by_id = {c["campaign_id"]: c for c in campaigns}
    assert by_id["1"]["planned_today"] == 554
    assert by_id["1"]["planned_start"] == 1000
    assert by_id["1"]["overdue_today"] == 446
    assert by_id["2"]["planned_today"] == 0
    assert by_id["2"]["planned_start"] == 500
    assert sum(c["planned_today"] for c in campaigns) == 554


def test_merge_live_includes_campaigns_missing_from_snapshot():
    ws = {"id": "ws_v1", "name": "Ottit V1"}
    snap_rows = [
        {"campaign_id": "1", "campaign_name": "Old", "planned": 10, "captured_at": "t"},
    ]
    live = [
        {
            "workspace_id": "ws_v1",
            "workspace_name": "Ottit V1",
            "campaign_id": "1",
            "campaign_name": "Old",
            "planned_today": 3,
            "overdue_today": 0,
            "inboxes": [],
            "error": None,
        },
        {
            "workspace_id": "ws_v1",
            "workspace_name": "Ottit V1",
            "campaign_id": "99",
            "campaign_name": "New",
            "planned_today": 7,
            "overdue_today": 0,
            "inboxes": [],
            "error": None,
        },
    ]

    campaigns, plan_total, _ = _merge_live_with_snapshot(ws, live, snap_rows)
    by_id = {c["campaign_id"]: c for c in campaigns}
    assert plan_total == 10
    assert by_id["99"]["planned_today"] == 7
    assert by_id["99"]["planned_start"] is None


def test_merge_workspaces_sums_to_all():
    v1 = {
        "date": "2026-08-01",
        "generated_at": "2026-08-01T12:00:00+00:00",
        "planned_total": 100,
        "remaining_total": 100,
        "overdue_total": 10,
        "plan_total": 200,
        "sent_total": 90,
        "approximate": False,
        "campaigns": [
            {"campaign_id": "a", "planned_today": 100, "workspace_id": "ws_v1"},
        ],
    }
    v2 = {
        "date": "2026-08-01",
        "generated_at": "2026-08-01T12:05:00+00:00",
        "planned_total": 5341,
        "remaining_total": 5341,
        "overdue_total": 446,
        "plan_total": 6000,
        "sent_total": 200,
        "approximate": True,
        "campaigns": [
            {"campaign_id": "b", "planned_today": 5341, "workspace_id": "ws_v2"},
        ],
    }

    all_ws = _merge_workspaces([v1, v2], "2026-08-01")

    assert all_ws["remaining_total"] == 100 + 5341
    assert all_ws["planned_total"] == 100 + 5341
    assert all_ws["plan_total"] == 200 + 6000
    assert all_ws["sent_total"] == 90 + 200
    assert all_ws["overdue_total"] == 10 + 446
    assert all_ws["approximate"] is True
    assert len(all_ws["campaigns"]) == 2
    assert all_ws["generated_at"] == "2026-08-01T12:05:00+00:00"
