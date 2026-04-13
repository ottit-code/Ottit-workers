"""Tests for workers/lead_engagement_poller.py."""
from unittest.mock import MagicMock, patch
import pytest

from workers.lead_engagement_poller import (
    _compute_engagement_score,
    _compute_funnel_stage,
    _build_custom_variables,
    poll_lead_engagement,
    run,
)

_MODULE = "workers.lead_engagement_poller"


class TestComputeEngagementScore:
    def test_all_zero(self):
        assert _compute_engagement_score(0, 0, 0) == 0

    def test_replies_weighted_highest(self):
        assert _compute_engagement_score(1, 0, 0) == 5

    def test_unique_opens_weighted_mid(self):
        assert _compute_engagement_score(0, 1, 0) == 2

    def test_opens_weighted_one(self):
        assert _compute_engagement_score(0, 0, 1) == 1

    def test_combined(self):
        # 2×5 + 3×2 + 4×1 = 10+6+4 = 20
        assert _compute_engagement_score(2, 3, 4) == 20


class TestComputeFunnelStage:
    def test_interested_wins_over_all(self):
        stage = _compute_funnel_stage(
            100, 50, 10,
            [{"interested": True, "emails_sent": 10}],
        )
        assert stage == "interested"

    def test_replied_when_no_interest(self):
        assert _compute_funnel_stage(10, 5, 1, []) == "replied"

    def test_opened_when_no_replies(self):
        assert _compute_funnel_stage(10, 5, 0, []) == "opened"

    def test_contacted_when_no_opens(self):
        assert _compute_funnel_stage(10, 0, 0, []) == "contacted"

    def test_uploaded_when_nothing(self):
        assert _compute_funnel_stage(0, 0, 0, []) == "uploaded"

    def test_interested_requires_campaign_flag(self):
        stage = _compute_funnel_stage(
            10, 5, 2,
            [{"interested": False}],
        )
        assert stage == "replied"


class TestBuildCustomVariables:
    def test_converts_list_to_dict(self):
        variables = [
            {"name": "company_size", "value": "50-100"},
            {"name": "industry", "value": "SaaS"},
        ]
        result = _build_custom_variables(variables)
        assert result == {"company_size": "50-100", "industry": "SaaS"}

    def test_empty_list(self):
        assert _build_custom_variables([]) == {}

    def test_none_input(self):
        assert _build_custom_variables(None) == {}

    def test_skips_entries_without_name(self):
        variables = [{"value": "orphan"}]
        assert _build_custom_variables(variables) == {}

    def test_uses_key_as_fallback_name(self):
        variables = [{"key": "alt_name", "value": "val"}]
        result = _build_custom_variables(variables)
        assert result == {"alt_name": "val"}


class TestPollLeadEngagement:
    def _make_supabase(self):
        sb = MagicMock()
        sb.table.return_value.upsert.return_value.execute.return_value.data = []
        return sb

    def _make_lead(self, lead_id="L1", emails_sent=5, opens=2, replies=1):
        return {
            "id": lead_id,
            "first_name": "Alice",
            "last_name": "Smith",
            "email": "alice@example.com",
            "title": "CEO",
            "company": "Acme",
            "status": "active",
            "tags": ["tag1"],
            "overall_stats": {
                "emails_sent": emails_sent,
                "opens": opens,
                "unique_opens": opens,
                "replies": replies,
                "unique_replies": replies,
            },
            "lead_campaign_data": [{"campaign_id": "c1", "interested": False}],
            "custom_variables": [{"name": "size", "value": "10"}],
        }

    def test_upserts_single_page(self):
        sb = self._make_supabase()
        page_response = {"data": [self._make_lead()], "meta": {"last_page": 1}}

        with (
            patch("lib.emailbison.get_leads_paginated", return_value=page_response),
            patch(f"{_MODULE}.get_supabase", return_value=sb),
        ):
            poll_lead_engagement()

        sb.table.assert_called_with("lead_engagement_snapshots")
        rows = sb.table.return_value.upsert.call_args[0][0]
        assert len(rows) == 1
        row = rows[0]
        assert row["lead_id"] == "L1"
        assert row["emails_sent"] == 5
        assert row["engagement_score"] == _compute_engagement_score(1, 2, 2)
        assert row["funnel_stage"] == "replied"
        assert row["custom_variables"] == {"size": "10"}

    def test_paginates_through_all_pages(self):
        sb = self._make_supabase()
        page1 = {"data": [self._make_lead("L1")], "meta": {"last_page": 2}}
        page2 = {"data": [self._make_lead("L2")], "meta": {"last_page": 2}}

        with (
            patch("lib.emailbison.get_leads_paginated", side_effect=[page1, page2]),
            patch(f"{_MODULE}.get_supabase", return_value=sb),
            patch("time.sleep"),
        ):
            poll_lead_engagement()

        assert sb.table.return_value.upsert.call_count == 2

    def test_stops_on_empty_page(self):
        sb = self._make_supabase()
        empty = {"data": [], "meta": {"last_page": 10}}

        with (
            patch("lib.emailbison.get_leads_paginated", return_value=empty),
            patch(f"{_MODULE}.get_supabase", return_value=sb),
        ):
            poll_lead_engagement()

        sb.table.return_value.upsert.assert_not_called()

    def test_handles_api_error_on_page(self):
        sb = self._make_supabase()
        with (
            patch("lib.emailbison.get_leads_paginated",
                  side_effect=Exception("rate limited")),
            patch(f"{_MODULE}.get_supabase", return_value=sb),
        ):
            poll_lead_engagement()  # Should not raise

        sb.table.return_value.upsert.assert_not_called()

    def test_handles_plain_list_response(self):
        sb = self._make_supabase()
        with (
            patch("lib.emailbison.get_leads_paginated",
                  return_value=[self._make_lead("L99")]),
            patch(f"{_MODULE}.get_supabase", return_value=sb),
        ):
            poll_lead_engagement()

        rows = sb.table.return_value.upsert.call_args[0][0]
        assert rows[0]["lead_id"] == "L99"


class TestRun:
    def test_run_does_not_raise_on_error(self):
        with patch(
            f"{_MODULE}.poll_lead_engagement",
            side_effect=Exception("crash"),
        ):
            run()
