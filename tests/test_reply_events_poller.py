"""Tests for workers/reply_events_poller.py."""
from unittest.mock import MagicMock, patch
import pytest

from workers.reply_events_poller import (
    _compute_response_time_hours,
    poll_reply_events,
    run,
)

_MODULE = "workers.reply_events_poller"


class TestComputeResponseTimeHours:
    def test_normal_calculation(self):
        result = _compute_response_time_hours(
            "2026-04-10T10:00:00Z",
            "2026-04-10T08:00:00Z",
        )
        assert result == pytest.approx(2.0)

    def test_fractional_hours(self):
        result = _compute_response_time_hours(
            "2026-04-10T08:30:00Z",
            "2026-04-10T08:00:00Z",
        )
        assert result == pytest.approx(0.5)

    def test_returns_none_for_missing_replied_at(self):
        assert _compute_response_time_hours(None, "2026-04-10T08:00:00Z") is None

    def test_returns_none_for_missing_sent_at(self):
        assert _compute_response_time_hours("2026-04-10T10:00:00Z", None) is None

    def test_returns_none_when_reply_before_sent(self):
        result = _compute_response_time_hours(
            "2026-04-10T07:00:00Z",
            "2026-04-10T08:00:00Z",
        )
        assert result is None

    def test_handles_timestamps_with_offset(self):
        result = _compute_response_time_hours(
            "2026-04-10T10:00:00+00:00",
            "2026-04-10T08:00:00+00:00",
        )
        assert result == pytest.approx(2.0)

    def test_returns_none_on_invalid_timestamp(self):
        result = _compute_response_time_hours("not-a-date", "2026-04-10T08:00:00Z")
        assert result is None


class TestPollReplyEvents:
    def _make_supabase(self):
        sb = MagicMock()
        sb.table.return_value.upsert.return_value.execute.return_value.data = []
        return sb

    def _make_reply(self, reply_id="R1"):
        return {
            "id": reply_id,
            "lead": {"id": "L1", "email": "lead@example.com"},
            "sender_email": {"id": "S1", "email": "sender@example.com"},
            "campaign": {"id": "C1", "name": "Test Campaign"},
            "scheduled_email": {
                "sequence_step_id": 10,
                "sent_at": "2026-04-10T08:00:00Z",
            },
            "replied_at": "2026-04-10T10:00:00Z",
            "subject": "Re: Hello",
            "folder": "inbox",
            "has_attachments": False,
            "thread_reply": False,
        }

    def test_upserts_classified_replies(self):
        sb = self._make_supabase()
        reply = self._make_reply()

        with (
            patch(f"{_MODULE}.get_active_campaign_ids_from_bison", return_value=["camp1"]),
            patch("lib.emailbison.get_campaign_replies",
                  side_effect=lambda cid, status: [reply] if status == "interested" else []),
            patch(f"{_MODULE}.get_supabase", return_value=sb),
            patch(f"{_MODULE}._existing_sent_map", return_value={}),
            patch(f"{_MODULE}._campaigns_with_unresolved_sent", return_value=[]),
        ):
            poll_reply_events()

        rows = sb.table.return_value.upsert.call_args[0][0]
        assert len(rows) == 1
        row = rows[0]
        assert row["reply_id"] == "R1"
        assert row["classification"] == "interested"
        assert row["response_time_hours"] == pytest.approx(2.0)
        assert row["lead_email"] == "lead@example.com"
        assert row["sender_email"] == "sender@example.com"

    def test_deduplicates_replies_across_classifications(self):
        sb = self._make_supabase()
        reply = self._make_reply("R1")

        with (
            patch(f"{_MODULE}.get_active_campaign_ids_from_bison", return_value=["camp1"]),
            patch("lib.emailbison.get_campaign_replies", return_value=[reply]),
            patch(f"{_MODULE}.get_supabase", return_value=sb),
            patch(f"{_MODULE}._existing_sent_map", return_value={}),
            patch(f"{_MODULE}._campaigns_with_unresolved_sent", return_value=[]),
        ):
            poll_reply_events()

        rows = sb.table.return_value.upsert.call_args[0][0]
        assert len(rows) == 1  # Deduplicated

    def test_collects_replies_from_all_campaigns(self):
        sb = self._make_supabase()

        def make_reply_for(cid, status):
            if status != "interested":
                return []
            return [self._make_reply(f"R_{cid}")]

        with (
            patch(f"{_MODULE}.get_active_campaign_ids_from_bison", return_value=["c1", "c2"]),
            patch("lib.emailbison.get_campaign_replies",
                  side_effect=lambda cid, s: make_reply_for(cid, s)),
            patch(f"{_MODULE}.get_supabase", return_value=sb),
            patch(f"{_MODULE}._existing_sent_map", return_value={}),
            patch(f"{_MODULE}._campaigns_with_unresolved_sent", return_value=[]),
        ):
            poll_reply_events()

        rows = sb.table.return_value.upsert.call_args[0][0]
        reply_ids = {r["reply_id"] for r in rows}
        assert reply_ids == {"R_c1", "R_c2"}

    def test_skips_replies_without_id(self):
        sb = self._make_supabase()
        reply = self._make_reply()
        reply["id"] = None

        with (
            patch(f"{_MODULE}.get_active_campaign_ids_from_bison", return_value=["c1"]),
            patch("lib.emailbison.get_campaign_replies", return_value=[reply]),
            patch(f"{_MODULE}.get_supabase", return_value=sb),
            patch(f"{_MODULE}._existing_sent_map", return_value={}),
            patch(f"{_MODULE}._campaigns_with_unresolved_sent", return_value=[]),
        ):
            poll_reply_events()

        sb.table.return_value.upsert.assert_not_called()

    def test_continues_on_classification_api_error(self):
        sb = self._make_supabase()

        def raises_for_interested(cid, status):
            if status == "interested":
                raise Exception("API error")
            return [self._make_reply(f"R_{status}")]

        with (
            patch(f"{_MODULE}.get_active_campaign_ids_from_bison", return_value=["c1"]),
            patch("lib.emailbison.get_campaign_replies",
                  side_effect=raises_for_interested),
            patch(f"{_MODULE}.get_supabase", return_value=sb),
            patch(f"{_MODULE}._existing_sent_map", return_value={}),
            patch(f"{_MODULE}._campaigns_with_unresolved_sent", return_value=[]),
        ):
            poll_reply_events()  # Should not raise

        rows = sb.table.return_value.upsert.call_args[0][0]
        assert len(rows) == 2  # not_automated_reply + automated_reply

    def test_no_campaigns_does_nothing(self):
        sb = self._make_supabase()
        with (
            patch(f"{_MODULE}.get_active_campaign_ids_from_bison", return_value=[]),
            patch(f"{_MODULE}.get_supabase", return_value=sb),
            patch(f"{_MODULE}._existing_sent_map", return_value={}),
            patch(f"{_MODULE}._campaigns_with_unresolved_sent", return_value=[]),
        ):
            poll_reply_events()

        sb.table.return_value.upsert.assert_not_called()


class TestRun:
    def test_run_does_not_raise_on_error(self):
        with patch(
            f"{_MODULE}.poll_reply_events",
            side_effect=Exception("crash"),
        ):
            run()
