"""Tests for workers/ab_test_snapshots_poller.py."""
from unittest.mock import MagicMock, patch, call
import pytest

from workers.ab_test_snapshots_poller import (
    _safe_rate,
    _aggregate_scheduled_emails,
    _empty_agg,
    _compute_significance,
    poll_ab_test_snapshots,
    run,
)

_MODULE = "workers.ab_test_snapshots_poller"


class TestSafeRate:
    def test_normal_calculation(self):
        assert _safe_rate(10, 100) == 10.0

    def test_zero_denominator_returns_zero(self):
        assert _safe_rate(5, 0) == 0.0

    def test_rounding(self):
        assert _safe_rate(1, 3) == pytest.approx(33.3333, rel=1e-3)


class TestAggregateScheduledEmails:
    def test_aggregates_by_step_id(self):
        emails = [
            {"sequence_step_id": 1, "status": "sent", "opens": 2, "unique_opens": 1,
             "clicks": 0, "replies": 1, "unique_replies": 1, "interested": True},
            {"sequence_step_id": 1, "status": "sent", "opens": 3, "unique_opens": 2,
             "clicks": 1, "replies": 0, "unique_replies": 0, "interested": False},
            {"sequence_step_id": 2, "status": "bounced", "opens": 0, "unique_opens": 0,
             "clicks": 0, "replies": 0, "unique_replies": 0, "interested": False},
        ]
        agg = _aggregate_scheduled_emails(emails)
        assert agg["1"]["emails_sent"] == 2
        assert agg["1"]["opens"] == 5
        assert agg["1"]["unique_opens"] == 3
        assert agg["1"]["clicks"] == 1
        assert agg["1"]["replies"] == 1
        assert agg["1"]["unique_replies"] == 1
        assert agg["1"]["interested"] == 1
        assert agg["1"]["bounced"] == 0
        assert agg["2"]["bounced"] == 1
        assert agg["2"]["emails_sent"] == 0

    def test_skips_emails_without_step_id(self):
        emails = [{"status": "sent", "opens": 1}]
        agg = _aggregate_scheduled_emails(emails)
        assert agg == {}

    def test_empty_input(self):
        assert _aggregate_scheduled_emails([]) == {}

    def test_handles_none_fields_gracefully(self):
        emails = [{"sequence_step_id": 5, "status": "sent", "opens": None,
                   "unique_opens": None, "clicks": None, "replies": None,
                   "unique_replies": None, "interested": None}]
        agg = _aggregate_scheduled_emails(emails)
        assert agg["5"]["opens"] == 0
        assert agg["5"]["emails_sent"] == 1


class TestComputeSignificance:
    def test_returns_none_tuple_when_rpc_unavailable(self):
        sb = MagicMock()
        sb.rpc.side_effect = Exception("RPC not found")
        result = _compute_significance(sb, {"unique_replies": 5, "emails_sent": 100}, {})
        assert result == (None, None, None)

    def test_extracts_values_from_dict_response(self):
        sb = MagicMock()
        sb.rpc.return_value.execute.return_value.data = {
            "stat_confidence": 95.0,
            "stat_winner": "variant",
            "stat_sample_sufficient": True,
        }
        result = _compute_significance(
            sb,
            {"unique_replies": 10, "emails_sent": 200},
            {"unique_replies": 20, "emails_sent": 200},
        )
        assert result == (95.0, "variant", True)

    def test_extracts_values_from_list_response(self):
        sb = MagicMock()
        sb.rpc.return_value.execute.return_value.data = [
            {"stat_confidence": 80.0, "stat_winner": "control", "stat_sample_sufficient": False}
        ]
        result = _compute_significance(sb, {}, {})
        assert result == (80.0, "control", False)


class TestPollAbTestSnapshots:
    def _make_supabase(self):
        sb = MagicMock()
        sb.table.return_value.upsert.return_value.execute.return_value.data = []
        sb.rpc.return_value.execute.return_value.data = None
        return sb

    def test_upserts_rows_for_each_step(self):
        sb = self._make_supabase()
        steps = [
            {"id": 1, "email_subject": "Hello", "order": 1, "variant": False,
             "variant_from_step_id": None, "thread_reply": False},
        ]
        emails = [
            {"sequence_step_id": 1, "status": "sent", "opens": 5, "unique_opens": 3,
             "clicks": 1, "replies": 2, "unique_replies": 2, "interested": True},
        ]
        with (
            patch(f"{_MODULE}.get_active_campaign_ids", return_value=["c1"]),
            patch("lib.emailbison.get_campaign_sequence_steps", return_value=steps),
            patch("lib.emailbison.get_campaign_scheduled_emails", return_value=emails),
            patch(f"{_MODULE}.get_supabase", return_value=sb),
        ):
            poll_ab_test_snapshots()

        sb.table.assert_called_with("ab_test_snapshots")
        upsert_call = sb.table.return_value.upsert.call_args
        rows = upsert_call[0][0]
        assert len(rows) == 1
        row = rows[0]
        assert row["sequence_step_id"] == 1
        assert row["emails_sent"] == 1
        assert row["open_rate"] == pytest.approx(300.0)   # 3/1 * 100
        assert row["reply_rate"] == pytest.approx(200.0)  # 2/1 * 100
        assert row["is_variant"] is False

    def test_skips_campaign_on_api_error(self):
        sb = self._make_supabase()
        with (
            patch(f"{_MODULE}.get_active_campaign_ids", return_value=["c1"]),
            patch("lib.emailbison.get_campaign_sequence_steps",
                  side_effect=Exception("API error")),
            patch(f"{_MODULE}.get_supabase", return_value=sb),
        ):
            poll_ab_test_snapshots()  # Should not raise

        sb.table.return_value.upsert.assert_not_called()

    def test_no_campaigns_does_nothing(self):
        sb = self._make_supabase()
        with (
            patch(f"{_MODULE}.get_active_campaign_ids", return_value=[]),
            patch(f"{_MODULE}.get_supabase", return_value=sb),
        ):
            poll_ab_test_snapshots()

        sb.table.return_value.upsert.assert_not_called()

    def test_variant_step_triggers_significance_rpc(self):
        sb = self._make_supabase()
        steps = [
            {"id": 1, "email_subject": "A", "order": 1, "variant": False,
             "variant_from_step_id": None, "thread_reply": False},
            {"id": 2, "email_subject": "B", "order": 1, "variant": True,
             "variant_from_step_id": 1, "thread_reply": False},
        ]
        emails = [
            {"sequence_step_id": 1, "status": "sent", "opens": 0, "unique_opens": 0,
             "clicks": 0, "replies": 5, "unique_replies": 5, "interested": False},
            {"sequence_step_id": 2, "status": "sent", "opens": 0, "unique_opens": 0,
             "clicks": 0, "replies": 3, "unique_replies": 3, "interested": False},
        ]
        with (
            patch(f"{_MODULE}.get_active_campaign_ids", return_value=["c1"]),
            patch("lib.emailbison.get_campaign_sequence_steps", return_value=steps),
            patch("lib.emailbison.get_campaign_scheduled_emails", return_value=emails),
            patch(f"{_MODULE}.get_supabase", return_value=sb),
        ):
            poll_ab_test_snapshots()

        sb.rpc.assert_called_once_with("compute_ab_significance", {
            "control_replies": 5,
            "control_sent": 1,
            "variant_replies": 3,
            "variant_sent": 1,
            "min_sample": 30,
        })


class TestRun:
    def test_run_calls_poll_and_does_not_raise_on_error(self):
        with patch(
            f"{_MODULE}.poll_ab_test_snapshots",
            side_effect=Exception("boom"),
        ):
            run()  # Should log error and not propagate
