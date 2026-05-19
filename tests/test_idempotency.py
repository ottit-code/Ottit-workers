"""Tests for reply_drafts_dao.claim — race-safe insert idempotency."""
from unittest.mock import MagicMock, patch

from lib import reply_drafts_dao
from models.bison_payload import BisonLeadInterestedData, BisonLead, BisonReply, BisonCampaign, BisonScheduledEmail, BisonSenderEmail


def _payload() -> BisonLeadInterestedData:
    return BisonLeadInterestedData(
        reply=BisonReply(
            id=91423,
            uuid="dup-uuid-1",
            text_body="hi",
            email_subject="Re: x",
            raw_message_id="<reply@x>",
            from_email_address="james@acme.io",
        ),
        lead=BisonLead(id=4567, email="james@acme.io"),
        campaign=BisonCampaign(id=1, name="c"),
        scheduled_email=BisonScheduledEmail(raw_message_id="<orig@x>"),
        sender_email=BisonSenderEmail(id=25065, email="saman@send.ottit.com"),
    )


def test_claim_returns_row_when_inserted():
    fake_row = {"id": "uuid-a", "bison_reply_uuid": "dup-uuid-1"}
    sb = MagicMock()
    sb.table.return_value.upsert.return_value.execute.return_value.data = [fake_row]
    with patch("lib.reply_drafts_dao.get_supabase", return_value=sb):
        out = reply_drafts_dao.claim(_payload())
    assert out == fake_row


def test_claim_returns_none_on_conflict():
    sb = MagicMock()
    # supabase-py with ignore_duplicates=True returns empty data on conflict
    sb.table.return_value.upsert.return_value.execute.return_value.data = []
    with patch("lib.reply_drafts_dao.get_supabase", return_value=sb):
        out = reply_drafts_dao.claim(_payload())
    assert out is None
