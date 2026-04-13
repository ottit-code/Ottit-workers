"""Tests for lib/utils.py."""
from unittest.mock import MagicMock, patch
from lib.utils import get_active_campaign_ids


def _make_supabase(rows):
    sb = MagicMock()
    (
        sb.table.return_value
        .select.return_value
        .eq.return_value
        .in_.return_value
        .execute.return_value
        .data
    ) = rows
    return sb


class TestGetActiveCampaignIds:
    def test_returns_ids_from_documents_table(self):
        rows = [
            {"metadata": {"campaign_id": "111", "type": "campaign", "campaign_status": "active"}},
            {"metadata": {"campaign_id": "222", "type": "campaign", "campaign_status": "queued"}},
        ]
        sb = _make_supabase(rows)
        result = get_active_campaign_ids(sb)
        assert set(result) == {"111", "222"}

    def test_deduplicates_campaign_ids(self):
        rows = [
            {"metadata": {"campaign_id": "111"}},
            {"metadata": {"campaign_id": "111"}},
        ]
        sb = _make_supabase(rows)
        result = get_active_campaign_ids(sb)
        assert result == ["111"]

    def test_skips_rows_without_campaign_id(self):
        rows = [
            {"metadata": {"type": "campaign"}},
            {"metadata": None},
            {"metadata": {"campaign_id": "333"}},
        ]
        sb = _make_supabase(rows)
        result = get_active_campaign_ids(sb)
        assert result == ["333"]

    def test_falls_back_to_emailbison_when_documents_empty(self):
        sb = _make_supabase([])
        campaigns = [
            {"id": "10", "status": "active"},
            {"id": "20", "status": "paused"},
            {"id": "30", "status": "completed"},  # should be excluded
        ]
        with patch("lib.emailbison.get_campaigns", return_value=campaigns):
            result = get_active_campaign_ids(sb)
        assert set(result) == {"10", "20"}

    def test_returns_empty_list_when_both_sources_fail(self):
        sb = MagicMock()
        sb.table.side_effect = Exception("db error")
        with patch("lib.emailbison.get_campaigns", side_effect=Exception("api error")):
            result = get_active_campaign_ids(sb)
        assert result == []

    def test_falls_back_to_emailbison_when_documents_raises(self):
        sb = MagicMock()
        sb.table.side_effect = Exception("connection refused")
        campaigns = [{"id": "99", "status": "active"}]
        with patch("lib.emailbison.get_campaigns", return_value=campaigns):
            result = get_active_campaign_ids(sb)
        assert result == ["99"]
