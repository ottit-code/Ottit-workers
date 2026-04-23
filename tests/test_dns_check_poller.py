"""Tests for workers/dns_check_poller.py."""
from unittest.mock import patch, MagicMock
import pytest


def test_check_spf_parses_record():
    from workers import dns_check_poller
    with patch.object(dns_check_poller, "_txt_records", return_value=["v=spf1 include:_spf.google.com ~all"]):
        passed, record = dns_check_poller._check_spf("example.com")
    assert passed is True
    assert "spf1" in record


def test_check_spf_fails_on_plus_all():
    from workers import dns_check_poller
    with patch.object(dns_check_poller, "_txt_records", return_value=["v=spf1 +all"]):
        passed, _ = dns_check_poller._check_spf("example.com")
    assert passed is False


def test_check_dkim_finds_first_matching_selector():
    from workers import dns_check_poller

    def fake_txt(name: str):
        if name.startswith("google._domainkey"):
            return ["v=DKIM1; k=rsa; p=MIGfMA0..."]
        return []

    with patch.object(dns_check_poller, "_txt_records", side_effect=fake_txt):
        passed, selector = dns_check_poller._check_dkim("example.com")
    assert passed is True
    assert selector == "google"


def test_check_dmarc_quarantine_passes():
    from workers import dns_check_poller
    with patch.object(dns_check_poller, "_txt_records", return_value=["v=DMARC1; p=quarantine; rua=mailto:a@b.c"]):
        passed, policy = dns_check_poller._check_dmarc("example.com")
    assert passed is True
    assert policy == "quarantine"


def test_check_dmarc_none_fails():
    from workers import dns_check_poller
    with patch.object(dns_check_poller, "_txt_records", return_value=["v=DMARC1; p=none"]):
        passed, policy = dns_check_poller._check_dmarc("example.com")
    assert passed is False
    assert policy == "none"


def test_poll_dns_health_writes_one_row_per_domain():
    from workers import dns_check_poller

    with patch.object(dns_check_poller, "_domains", return_value=["a.com", "b.com"]), \
         patch.object(dns_check_poller, "_check_spf", return_value=(True, "v=spf1 ~all")), \
         patch.object(dns_check_poller, "_check_dkim", return_value=(True, "default")), \
         patch.object(dns_check_poller, "_check_dmarc", return_value=(False, "none")), \
         patch.object(dns_check_poller, "get_supabase") as mock_get_sb:
        mock_sb = MagicMock()
        mock_get_sb.return_value = mock_sb
        dns_check_poller.poll_dns_health()

    insert = mock_sb.table.return_value.insert
    rows = insert.call_args[0][0]
    assert len(rows) == 2
    assert {r["domain"] for r in rows} == {"a.com", "b.com"}
    assert all(r["spf_passed"] is True for r in rows)
    assert all(r["dmarc_passed"] is False for r in rows)
