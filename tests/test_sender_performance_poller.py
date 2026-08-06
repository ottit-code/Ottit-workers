"""Tests for workers/sender_performance_poller.py."""
from unittest.mock import MagicMock, patch
import pytest

from workers.sender_performance_poller import (
    _safe_rate,
    _fetch_sender_lookup_data,
    _warmup_counters,
    poll_sender_email_performance,
    run,
)

_MODULE = "workers.sender_performance_poller"


def _perf_upsert_rows(sb):
    """First list upsert is sender_email_performance (warmup report upserts a dict)."""
    for call in sb.table.return_value.upsert.call_args_list:
        payload = call[0][0]
        if isinstance(payload, list):
            return payload
    raise AssertionError("No list upsert found (sender_email_performance)")


def _mock_bison(accounts=None, side_effect=None):
    """Mock BisonClient — the poller resolves clients via for_workspace(),
    so module-level function patches never intercept its calls."""
    client = MagicMock()
    if side_effect is not None:
        client.get_campaign_email_accounts.side_effect = side_effect
    else:
        client.get_campaign_email_accounts.return_value = accounts or []
    return client


class TestSafeRate:
    def test_normal(self):
        assert _safe_rate(10, 200) == 5.0

    def test_zero_denominator(self):
        assert _safe_rate(5, 0) == 0.0


class TestWarmupCounters:
    def test_maps_bison_warmup_fields(self):
        mapped = _warmup_counters({
            "warmup_emails_sent": 108,
            "warmup_replies_received": 12,
            "warmup_emails_saved_from_spam": 24,
            "warmup_bounces_received_count": 1,
            "warmup_bounces_caused_count": 2,
            "warmup_score": 77.78,
        })
        assert mapped == {
            "warmup_sent": 108,
            "warmup_replied": 12,
            "warmup_saved_from_spam": 24,
            "warmup_bounces_received": 1,
            "warmup_bounces_caused": 2,
        }

    def test_missing_fields_stay_none(self):
        assert _warmup_counters({}) == {
            "warmup_sent": None,
            "warmup_replied": None,
            "warmup_saved_from_spam": None,
            "warmup_bounces_received": None,
            "warmup_bounces_caused": None,
        }


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
            patch("lib.emailbison.for_workspace", return_value=_mock_bison([account])),
            patch(f"{_MODULE}.get_supabase", return_value=sb),
        ):
            poll_sender_email_performance()

        rows = _perf_upsert_rows(sb)
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
            patch("lib.emailbison.for_workspace", return_value=_mock_bison([account])),
            patch(f"{_MODULE}.get_supabase", return_value=sb),
        ):
            poll_sender_email_performance()

        row = _perf_upsert_rows(sb)[0]
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
            patch("lib.emailbison.for_workspace", return_value=_mock_bison([account])),
            patch(f"{_MODULE}.get_supabase", return_value=sb),
        ):
            poll_sender_email_performance()

        row = _perf_upsert_rows(sb)[0]
        assert row["domain"] == "mycompany.io"

    def test_persists_warmup_daily_report_after_performance(self):
        sb = self._make_supabase()
        account = self._make_account()

        with (
            patch(f"{_MODULE}.get_active_campaign_ids", return_value=["c1"]),
            patch("lib.emailbison.for_workspace", return_value=_mock_bison([account])),
            patch(f"{_MODULE}.get_supabase", return_value=sb),
            patch(f"{_MODULE}.persist_warmup_daily_report") as persist,
        ):
            poll_sender_email_performance()

        persist.assert_called_once()
        assert persist.call_args.args[0] == "ws_v1"
        assert isinstance(persist.call_args.kwargs["rows"], list)
        assert len(persist.call_args.kwargs["rows"]) == 1

    def test_persists_live_warmup_counters_on_performance_row(self):
        sb = self._make_supabase()
        account = self._make_account(sender_id=42)
        bison = _mock_bison([account])
        bison.get_warmup_sender_emails.return_value = [
            {
                "id": 42,
                "email": "s@example.com",
                "domain": "example.com",
                "tags": ["CI-DED-SET1"],
                "warmup_emails_sent": 108,
                "warmup_replies_received": 12,
                "warmup_emails_saved_from_spam": 24,
                "warmup_score": 77.8,
                "warmup_bounces_received_count": 0,
                "warmup_bounces_caused_count": 1,
            }
        ]

        with (
            patch(f"{_MODULE}.get_active_campaign_ids", return_value=["c1"]),
            patch("lib.emailbison.for_workspace", return_value=bison),
            patch(f"{_MODULE}.get_supabase", return_value=sb),
            patch(f"{_MODULE}._record_warmup_history"),
            patch(f"{_MODULE}.persist_warmup_daily_report"),
        ):
            poll_sender_email_performance()

        row = _perf_upsert_rows(sb)[0]
        assert row["warmup_score"] == 78  # rounded int
        assert row["warmup_sent"] == 108
        assert row["warmup_replied"] == 12
        assert row["warmup_saved_from_spam"] == 24
        assert row["warmup_bounces_received"] == 0
        assert row["warmup_bounces_caused"] == 1
        assert row["tags"] == ["CI-DED-SET1"]

    def test_skips_accounts_without_id(self):
        sb = self._make_supabase()
        account = self._make_account()
        account["id"] = None

        with (
            patch(f"{_MODULE}.get_active_campaign_ids", return_value=["c1"]),
            patch("lib.emailbison.for_workspace", return_value=_mock_bison([account])),
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
            patch("lib.emailbison.for_workspace", return_value=_mock_bison(side_effect=side_effect)),
            patch(f"{_MODULE}.get_supabase", return_value=sb),
        ):
            poll_sender_email_performance()

        # Performance batch upsert still happened despite the bad campaign.
        assert len(_perf_upsert_rows(sb)) == 1


class TestRun:
    def test_run_does_not_raise_on_error(self):
        with patch(
            f"{_MODULE}.poll_sender_email_performance",
            side_effect=Exception("crash"),
        ):
            run()
