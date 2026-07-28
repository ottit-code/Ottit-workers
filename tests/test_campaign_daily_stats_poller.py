"""Tests for workers/campaign_daily_stats_poller.py."""
from unittest.mock import MagicMock, patch
import pytest

from workers.campaign_daily_stats_poller import (
    _safe_rate,
    _parse_chart_stats,
    poll_campaign_daily_stats,
    run,
)

_MODULE = "workers.campaign_daily_stats_poller"


def _mock_bison(details=None, chart=None, stats=None, details_side_effect=None,
                chart_side_effect=None, stats_side_effect=None):
    """Mock BisonClient — the poller resolves clients via for_workspace(),
    so module-level function patches never intercept its calls."""
    from unittest.mock import MagicMock
    client = MagicMock()
    if details_side_effect is not None:
        client.get_campaign_details.side_effect = details_side_effect
    else:
        client.get_campaign_details.return_value = details or {}
    if chart_side_effect is not None:
        client.get_campaign_line_area_chart_stats.side_effect = chart_side_effect
    else:
        client.get_campaign_line_area_chart_stats.return_value = chart or {}
    if stats_side_effect is not None:
        client.get_campaign_stats.side_effect = stats_side_effect
    else:
        client.get_campaign_stats.return_value = stats or {}
    return client


class TestSafeRate:
    def test_normal(self):
        assert _safe_rate(25, 100) == 25.0

    def test_zero_denominator(self):
        assert _safe_rate(10, 0) == 0.0

    def test_float_inputs(self):
        assert _safe_rate(1.0, 3.0) == pytest.approx(33.3333, rel=1e-3)


class TestParseChartStats:
    def _make_series(self):
        return [
            {"label": "Sent", "dates": [["2026-04-10", 100], ["2026-04-11", 120]]},
            {"label": "Total Opens", "dates": [["2026-04-10", 40], ["2026-04-11", 50]]},
            {"label": "Unique Opens", "dates": [["2026-04-10", 30], ["2026-04-11", 40]]},
            {"label": "Replied", "dates": [["2026-04-10", 10], ["2026-04-11", 15]]},
            {"label": "Bounced", "dates": [["2026-04-10", 2], ["2026-04-11", 3]]},
            {"label": "Unsubscribed", "dates": [["2026-04-10", 1], ["2026-04-11", 0]]},
            {"label": "Interested", "dates": [["2026-04-10", 5], ["2026-04-11", 8]]},
        ]

    def test_parses_dict_response(self):
        raw = {"data": self._make_series()}
        result = _parse_chart_stats(raw)
        assert "2026-04-10" in result
        assert result["2026-04-10"]["emails_sent"] == 100
        assert result["2026-04-10"]["emails_opened"] == 40
        assert result["2026-04-10"]["unique_opens"] == 30
        assert result["2026-04-10"]["emails_replied"] == 10
        assert result["2026-04-10"]["emails_bounced"] == 2
        assert result["2026-04-10"]["unsubscribed"] == 1
        assert result["2026-04-10"]["interested"] == 5

    def test_parses_list_response(self):
        result = _parse_chart_stats(self._make_series())
        assert "2026-04-11" in result
        assert result["2026-04-11"]["emails_sent"] == 120

    def test_ignores_unknown_labels(self):
        raw = {"data": [{"label": "Unknown Metric", "dates": [["2026-04-10", 999]]}]}
        result = _parse_chart_stats(raw)
        assert result == {}

    def test_empty_series(self):
        assert _parse_chart_stats({"data": []}) == {}

    def test_handles_none_count(self):
        raw = {"data": [{"label": "Sent", "dates": [["2026-04-10", None]]}]}
        result = _parse_chart_stats(raw)
        assert result["2026-04-10"]["emails_sent"] == 0


class TestPollCampaignDailyStats:
    def _make_supabase(self, has_existing_rows=False):
        sb = MagicMock()
        sb.table.return_value.select.return_value.eq.return_value \
            .limit.return_value.execute.return_value.data = (
            [{"stat_date": "2026-01-01"}] if has_existing_rows else []
        )
        sb.table.return_value.upsert.return_value.execute.return_value.data = []
        return sb

    def _chart_stats(self):
        return {"data": [
            {"label": "Sent", "dates": [["2026-04-10", 50]]},
            {"label": "Unique Opens", "dates": [["2026-04-10", 10]]},
            {"label": "Replied", "dates": [["2026-04-10", 5]]},
            {"label": "Bounced", "dates": [["2026-04-10", 1]]},
        ]}

    def test_upserts_rows_per_date(self):
        sb = self._make_supabase(has_existing_rows=True)
        details = {"name": "Test Campaign", "status": "active"}

        with (
            patch(f"{_MODULE}.get_active_campaign_ids", return_value=["c1"]),
            patch("lib.emailbison.for_workspace",
                  return_value=_mock_bison(details=details, chart=self._chart_stats())),
            patch(f"{_MODULE}.get_supabase", return_value=sb),
        ):
            poll_campaign_daily_stats()

        sb.table.assert_any_call("campaign_daily_stats")
        upsert_call = sb.table.return_value.upsert.call_args
        rows = upsert_call[0][0]
        assert len(rows) == 1
        row = rows[0]
        assert row["campaign_id"] == "c1"
        assert row["campaign_name"] == "Test Campaign"
        assert row["emails_sent"] == 50
        assert row["open_rate"] == pytest.approx(20.0)   # 10/50*100
        assert row["reply_rate"] == pytest.approx(10.0)  # 5/50*100
        assert row["bounce_rate"] == pytest.approx(2.0)  # 1/50*100

    def test_first_run_uses_created_at_as_start_date(self):
        sb = self._make_supabase(has_existing_rows=False)
        details = {"name": "New Camp", "status": "active", "created_at": "2026-01-15T00:00:00Z"}

        client = _mock_bison(details=details, chart={"data": []})
        chart_mock = client.get_campaign_line_area_chart_stats
        with (
            patch(f"{_MODULE}.get_active_campaign_ids", return_value=["c1"]),
            patch("lib.emailbison.for_workspace", return_value=client),
            patch(f"{_MODULE}.get_supabase", return_value=sb),
        ):
            poll_campaign_daily_stats()

        call_args = chart_mock.call_args
        assert call_args[0][1] == "2026-01-15"  # start_date derived from created_at

    def test_skips_campaign_on_api_error(self):
        sb = self._make_supabase()
        with (
            patch(f"{_MODULE}.get_active_campaign_ids", return_value=["c1"]),
            patch("lib.emailbison.for_workspace",
                  return_value=_mock_bison(details_side_effect=Exception("API down"))),
            patch(f"{_MODULE}.get_supabase", return_value=sb),
        ):
            poll_campaign_daily_stats()

        sb.table.return_value.upsert.assert_not_called()

    def test_summary_stats_failure_does_not_abort_row(self):
        sb = self._make_supabase(has_existing_rows=True)
        details = {"name": "Camp", "status": "active"}

        with (
            patch(f"{_MODULE}.get_active_campaign_ids", return_value=["c1"]),
            patch("lib.emailbison.for_workspace",
                  return_value=_mock_bison(
                      details=details, chart=self._chart_stats(),
                      stats_side_effect=Exception("stats unavailable"))),
            patch(f"{_MODULE}.get_supabase", return_value=sb),
        ):
            poll_campaign_daily_stats()

        # Should still upsert even without summary stats
        sb.table.return_value.upsert.assert_called_once()


class TestRun:
    def test_run_does_not_raise_on_error(self):
        with patch(
            f"{_MODULE}.poll_campaign_daily_stats",
            side_effect=Exception("crash"),
        ):
            run()
