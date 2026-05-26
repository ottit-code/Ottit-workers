"""
Tests for POST /admin/draft-test — the convenience endpoint that lets you
drive the drafter with a raw email body instead of a full Bison payload.

We don't need a fresh fake Supabase/Anthropic/OpenAI here — we test:
  1. Auth boundary (missing/wrong bearer => 401).
  2. Validation (empty body => 422).
  3. The synthetic payload builder maps fields correctly into the
     BisonLeadInterestedData shape the drafter pipeline expects.
  4. A monkey-patched `drafter.run` confirms our endpoint hands off the
     synthetic payload and reformats the result into the public DraftResponse.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api.routers.drafter_admin import DraftTestRequest, _build_synthetic_payload
from models.bison_payload import BisonLeadInterestedData
from models.drafts import ConfidenceComponents, DraftResult, SlackPayload

ADMIN_KEY = "test-admin-key"


@pytest.fixture
def client():
    from api.main import app
    return TestClient(app)


# --- payload synthesis ---------------------------------------------------

def test_build_synthetic_payload_minimal_body_only():
    """A request with only `email_body` should still produce a valid payload."""
    req = DraftTestRequest(email_body="Hey, sounds interesting. What's pricing look like?")
    payload = _build_synthetic_payload(req)

    assert isinstance(payload, BisonLeadInterestedData)
    assert payload.reply.text_body == req.email_body
    assert payload.reply.uuid  # fresh uuid minted
    assert payload.reply.email_subject == "Re: quick question"
    assert payload.reply.from_email_address == "test-lead@example.com"
    assert payload.reply.from_name == "Friend"
    assert payload.lead.first_name == "Friend"
    assert payload.lead.last_name is None
    assert payload.sender_email.email == "saman@ottit.com"
    assert payload.campaign.name == "Manual draft test"


def test_build_synthetic_payload_passes_through_provided_context():
    req = DraftTestRequest(
        email_body="Quick question on pilot pricing for a 200-person team.",
        subject="Re: monthly close drag",
        lead_first_name="James",
        lead_last_name="Holt",
        lead_email="james@acmehealth.io",
        lead_company="Acme Healthcare",
        lead_title="CFO",
        lead_id=4567,
        sender_email="saman@ottit.com",
        sender_email_id=332,
        campaign_id=25065,
        campaign_name="Monthly close acceleration Q2",
        custom_variables={"industry": "Healthcare SaaS"},
    )
    payload = _build_synthetic_payload(req)

    assert payload.reply.from_email_address == "james@acmehealth.io"
    assert payload.reply.from_name == "James Holt"
    assert payload.lead.id == 4567
    assert payload.lead.company == "Acme Healthcare"
    assert payload.lead.title == "CFO"
    assert payload.sender_email.id == 332
    assert payload.campaign.id == 25065
    cv = payload.lead.custom_variables
    assert len(cv) == 1 and cv[0].name == "industry" and cv[0].value == "Healthcare SaaS"


def test_build_synthetic_payload_mints_unique_uuids():
    """Every call must produce a fresh uuid so duplicate test calls draft fresh."""
    req = DraftTestRequest(email_body="hi")
    a = _build_synthetic_payload(req)
    b = _build_synthetic_payload(req)
    assert a.reply.uuid != b.reply.uuid


# --- auth + validation ----------------------------------------------------

def test_endpoint_rejects_missing_bearer(client):
    resp = client.post("/admin/draft-test", json={"email_body": "hello"})
    assert resp.status_code == 401


def test_endpoint_rejects_wrong_bearer(client):
    resp = client.post(
        "/admin/draft-test",
        headers={"Authorization": "Bearer wrong"},
        json={"email_body": "hello"},
    )
    assert resp.status_code == 401


def test_endpoint_rejects_empty_body(client):
    resp = client.post(
        "/admin/draft-test",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        json={"email_body": ""},
    )
    assert resp.status_code == 422


# --- pipeline pass-through ------------------------------------------------

def test_endpoint_returns_drafter_result(client):
    """Mock drafter.run; assert the endpoint forwards a synthetic payload and
    repackages the result into the public DraftResponse shape."""
    captured = {}

    def fake_run(payload):
        # The drafter receives our synthetic payload, not a Bison-shaped one.
        captured["payload"] = payload
        return DraftResult(
            draft_id="test-draft-123",
            bison_reply_uuid=payload.reply.uuid,
            duplicate=False,
            subject="Re: quick question",
            body="Hi Friend,\n\nThanks — happy to help.\n\nCheers,\nSaman",
            human_review_needed=False,
            review_reason="",
            confidence=ConfidenceComponents(
                llm_self_rating=0.8,
                rule_gate_pass=True,
                rule_gates_failed=[],
                ensemble_agreement=0.9,
                rag_retrieval_quality=0.5,
                composite=0.78,
            ),
            rag_examples_used=[],
            model_primary="claude-opus-4-7",
            model_ensemble="claude-haiku-4-5",
            slack=SlackPayload(text="*test*", blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "x"}}]),
            clean_prospect_reply="Quick pricing question.",
        )

    with patch("api.routers.drafter_admin.drafter.run", side_effect=fake_run):
        resp = client.post(
            "/admin/draft-test",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={
                "email_body": "Quick pricing question.",
                "lead_first_name": "James",
                "lead_email": "james@acmehealth.io",
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["received"] is True
    assert body["draft_id"] == "test-draft-123"
    assert body["skipped_reason"] is None
    assert body["draft"]["subject"].lower().startswith("re:")
    assert "Cheers" in body["draft"]["body"]
    assert body["confidence"]["composite"] == pytest.approx(0.78)
    assert body["clean_prospect_reply"] == "Quick pricing question."
    assert body["slack"]["text"] == "*test*"

    # Synthetic payload was built correctly: lead + reply fields propagated.
    p = captured["payload"]
    assert p.reply.text_body == "Quick pricing question."
    assert p.lead.first_name == "James"
    assert p.lead.email == "james@acmehealth.io"
    assert p.reply.uuid == body["bison_reply_uuid"]


def test_endpoint_propagates_drafter_failure_as_500(client):
    from lib import drafter as _drafter

    def boom(_payload):
        raise _drafter.DrafterError("pipeline blew up")

    with patch("api.routers.drafter_admin.drafter.run", side_effect=boom):
        resp = client.post(
            "/admin/draft-test",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"email_body": "x"},
        )
    assert resp.status_code == 500
    assert "pipeline blew up" in resp.json()["detail"]
