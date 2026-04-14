"""Tests for workers/sender_performance_poller.py."""
from unittest.mock import MagicMock, patch
import pytest

from workers.sender_performance_poller import (
    _safe_rate,
    _fetch_sender_lookup_data,
    poll_sender_email_performance,
    run,
)

_MODULE = "workers.sender_performance_poller"


class TestSafeRate:
    def test_normal(self):
        assert _safe_rate(10, 200) == 5.0

    def test_zero_denominator(self):
        assert _safe_rate(5, 0) == 0.0


class TestFetchSenderLookupData:
    def _make_sb(self):
        sb = MagicMock()
        chain = MagicMock()
        chain.select.return_value.in_.return_value.order.return_value \
            .execute.return_value.data = []
        chain.select.return_value.in_.return_value.is_.return_value \
            .execute.return_value.data = []
        sb.table.return_value = chain
        return sb

    def test_returns_empty_dict_for_no_senders(self):
        sb = MagicMock()
        result = _fetch_sender_lookup_data(sb, [])
        assert result == {}

    def test_initialises_all_fields_for_each_sender(self):
        sb = self._make_sb()
        result = _fetch_sender_lookup_data(sb, [101, 202])
        assert set(result.keys()) == {101, 202}
        for sid in [101, 202]:
            assert result[sid]["warmup_score"] is None
            assert result[sid]["in_recovery"] is False
            assert result[sid]["placement_score"] is None
            assert result[sid]["spam_score"] is None

    def test_continues_when_a_table_query_fails(self):
        sb = MagicMock()
        sb.table.side_effect = Exception("DB timeout")
        result = _fetch_sender_lookup_data(sb, [10])
        assert result[10]["warmup_score"] is None


class TestPollSenderEmailPerformance:
    def _make_supabase(self):
        sb = MagicMock()
        chain = MagicMock()
        chain.select.return_value.in_.return_value.order.return_value \
            .execute.return_value.data = []
        chain.select.return_value.in_.return_value.is_.return_value \
            .execute.return_value.data = []
        chain.upsert.return_value.execute.return_value.data = []
        sb.table.return_value = chain
        sb.rpc.return_value.execute.return_value.data = None
        return sb

    def _make_account(self, sender_id=1, email="s@example.com"):
        return {
            "id": sender_id,
            "email": email,
            "domain": "example.com",
            "type": "gmail",
            "status": "connected",
            "warmup_enabled": True,
            "emails_sent_count": 1000,
            "total_leads_contacted_count": 900,
            "total_replied_count": 100,
            "total_opened_count": 300,
            "unique_replied_count": 80,
            "unique_opened_count": 250,
            "unsubscribed_count": 5,
            "bounced_count": 20,
            "interested_leads_count": 30,
            "tags": [],
        }

    def test_upserts_one_row_per_unique_sender(self):
        sb = self._make_supabase()
        account = self._make_account()

        with (
            patch(f"{_MODULE}.get_active_campaign_ids", return_value=["c1", "c2"]),
            patch("lib.emailbison.get_campaign_email_accounts", return_value=[account]),
            patch(f"{_MODULE}.get_supabase", return_value=sb),
        ):
            poll_sender_email_performance()

        rows = sb.table.return_value.upsert.call_args[0][0]
        assert len(rows) == 1  # Deduplicated across 2 campaigns
        row = rows[0]
        assert row["sender_email_id"] == 1
        assert row["sender_email"] == "s@example.com"
        assert row["connection_type"] == "gmail"
        assert row["connection_status"] == "connected"
        assert row["emails_sent_count"] == 1000

    def test_computes_rates_correctly(self):
        sb = self._make_supabase()
        account = self._make_account()

        with (
            patch(f"{_MODULE}.get_active_campaign_ids", return_value=["c1"]),
            patch("lib.emailbison.get_campaign_email_accounts", return_value=[account]),
            patch(f"{_MODULE}.get_supabase", return_value=sb),
        ):
            poll_sender_email_performance()

        row = sb.table.return_value.upsert.call_args[0][0][0]
        # reply_rate = unique_replied / leads_contacted * 100
        assert row["reply_rate"] == pytest.approx(80 / 900 * 100, rel=1e-3)
        # bounce_rate = bounced / emails_sent * 100
        assert row["bounce_rate"] == pytest.approx(20 / 1000 * 100, rel=1e-3)

    def test_deduces_domain_from_email(self):
        sb = self._make_supabase()
        account = self._make_account(email="sender@mycompany.io")
        account.pop("domain", None)

        with (
            patch(f"{_MODULE}.get_active_campaign_ids", return_value=["c1"]),
            patch("lib.emailbison.get_campaign_email_accounts", return_value=[account]),
            patch(f"{_MODULE}.get_supabase", return_value=sb),
        ):
            poll_sender_email_performance()

        row = sb.table.return_value.upsert.call_args[0][0][0]
        assert row["domain"] == "mycompany.io"

    def test_skips_accounts_without_id(self):
        sb = self._make_supabase()
        account = self._make_account()
        account["id"] = None

        with (
            patch(f"{_MODULE}.get_active_campaign_ids", return_value=["c1"]),
            patch("lib.emailbison.get_campaign_email_accounts", return_value=[account]),
            patch(f"{_MODULE}.get_supabase", return_value=sb),
        ):
            poll_sender_email_performance()

        sb.table.return_value.upsert.assert_not_called()

    def test_no_campaigns_does_nothing(self):
        sb = self._make_supabase()
        with (
            patch(f"{_MODULE}.get_active_campaign_ids", return_value=[]),
            patch(f"{_MODULE}.get_supabase", return_value=sb),
        ):
            poll_sender_email_performance()

        sb.table.return_value.upsert.assert_not_called()

    def test_continues_on_campaign_api_error(self):
        sb = self._make_supabase()
        account = self._make_account()

        def side_effect(cid):
            if cid == "bad":
                raise Exception("API error")
            return [account]

        with (
            patch(f"{_MODULE}.get_active_campaign_ids", return_value=["good", "bad"]),
            patch("lib.emailbison.get_campaign_email_accounts", side_effect=side_effect),
            patch(f"{_MODULE}.get_supabase", return_value=sb),
        ):
            poll_sender_email_performance()

        sb.table.return_value.upsert.assert_called_once()


class TestRun:
    def test_run_does_not_raise_on_error(self):
        with patch(
            f"{_MODULE}.poll_sender_email_performance",
            side_effect=Exception("crash"),
        ):
            run()
