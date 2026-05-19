"""
End-to-end test for POST /webhooks/bison/lead-interested.

Mocks Anthropic, OpenAI, and Supabase. The real fixture payload is fed in
and we assert:
  - 200 response with the expected draft JSON shape
  - Confidence components populated
  - Idempotency: a duplicate call returns the same draft with skipped_reason='duplicate'
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "lead_interested.json"
DRAFTER_KEY = "test-drafter-key"


def _ok_draft_body() -> str:
    return (
        "Hi James,\n\n"
        "Appreciate the context — monthly close drag at a 200-person SaaS is a "
        "very specific pain we work on regularly. Without quoting numbers, the "
        "pilot for a team your size typically runs for two weeks and focuses on "
        "the AP/AR handoff that breaks the close window. Want to do a 15-min "
        "call to see if it's the right fit? Calendar: https://cal.com/saman/intro\n\n"
        "Best,\nSaman"
    )


def _claude_json(body: str | None = None) -> str:
    return json.dumps({
        "subject": "Re: monthly close drag at Acme?",
        "body": body or _ok_draft_body(),
        "confidence": 0.85,
        "human_review_needed": False,
        "review_reason": "",
    })


class _FakeBlock:
    def __init__(self, text: str):
        self.text = text


class _FakeMessage:
    def __init__(self, text: str):
        self.content = [_FakeBlock(text)]


class _FakeAnthropic:
    def __init__(self, *_, **__):
        self.messages = self
        self.calls = 0

    def create(self, *, model, max_tokens, temperature, system, messages):
        self.calls += 1
        return _FakeMessage(_claude_json())


class _FakeEmbeddingsData:
    def __init__(self, vec):
        self.embedding = vec


class _FakeEmbeddingsResp:
    def __init__(self, vecs):
        self.data = [_FakeEmbeddingsData(v) for v in vecs]


class _FakeOpenAI:
    def __init__(self, *_, **__):
        self.embeddings = self

    def create(self, *, model, input):
        if isinstance(input, str):
            return _FakeEmbeddingsResp([[0.01] * 1536])
        return _FakeEmbeddingsResp([[0.01] * 1536 for _ in input])


@pytest.fixture
def client():
    from api.main import app
    return TestClient(app)


@pytest.fixture
def fixture_payload():
    return json.loads(FIXTURE_PATH.read_text())


@pytest.fixture
def patches():
    """Install all the mocks the drafter pipeline needs."""
    fake_anthropic = _FakeAnthropic()

    # Supabase mock that:
    #  - claims by upserting and returning the row (winning insert)
    #  - finalizes (update) without error
    #  - returns the just-written row on get_by_uuid for duplicate scenarios
    sb = MagicMock()
    written = {}

    def upsert_side_effect(row, **_kw):
        # First call wins — record the row. Subsequent calls return empty.
        if "bison_reply_uuid" in row and row["bison_reply_uuid"] not in written:
            written[row["bison_reply_uuid"]] = dict(row)
            execute = MagicMock()
            execute.execute.return_value.data = [row]
            return execute
        execute = MagicMock()
        execute.execute.return_value.data = []
        return execute

    def select_eq_uuid_side_effect(_col, val):
        chain = MagicMock()
        chain.limit.return_value.execute.return_value.data = [written[val]] if val in written else []
        return chain

    # Build the supabase chain. We rely on a small handful of methods.
    table_mock = MagicMock()
    table_mock.upsert.side_effect = upsert_side_effect
    # update().eq().execute()
    table_mock.update.return_value.eq.return_value.execute.return_value.data = []
    # insert(...).execute() (used by audit + rag.insert_voice_example)
    table_mock.insert.return_value.execute.return_value.data = [{"id": 1}]
    # delete(...).eq().execute()
    table_mock.delete.return_value.eq.return_value.execute.return_value.data = []
    # select used by get_by_uuid + lead_enricher
    select_mock = MagicMock()
    select_mock.eq.side_effect = select_eq_uuid_side_effect
    select_mock.order.return_value.limit.return_value.execute.return_value.data = []
    table_mock.select.return_value = select_mock

    sb.table.return_value = table_mock
    sb.rpc.return_value.execute.return_value.data = []  # no RAG examples
    sb.storage.from_.return_value.download.side_effect = Exception("storage offline in test")

    with patch("anthropic.Anthropic", return_value=fake_anthropic), \
         patch("openai.OpenAI", return_value=_FakeOpenAI()), \
         patch("lib.supabase_client.get_supabase", return_value=sb), \
         patch("lib.reply_drafts_dao.get_supabase", return_value=sb), \
         patch("lib.audit.get_supabase", return_value=sb), \
         patch("lib.rag.get_supabase", return_value=sb), \
         patch("lib.lead_enricher.get_supabase", return_value=sb), \
         patch("lib.voice_loader.get_voice_loader") as voice_loader_mock:
        loader = MagicMock()
        loader.get.return_value = (
            "You are Saman. Hard rules: greet with 'Hi <name>,', sign 'Best,\\nSaman'.",
            "",
        )
        voice_loader_mock.return_value = loader

        # Make sure module-level _client singletons get re-instantiated.
        import lib.anthropic_client as ac
        import lib.openai_embed as oe
        ac._client = None
        oe._client = None

        yield {"supabase": sb, "anthropic": fake_anthropic, "written": written}


def test_drafter_inbound_returns_draft(client, fixture_payload, patches):
    resp = client.post(
        "/webhooks/bison/lead-interested",
        headers={"Authorization": f"Bearer {DRAFTER_KEY}"},
        json=fixture_payload,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["received"] is True
    assert body["bison_reply_uuid"] == "31a04c41-1bf6-4dab-a0a8-bb2a90b89195"
    assert body["skipped_reason"] is None
    assert body["draft"]["subject"].lower().startswith("re:")
    assert "Hi James" in body["draft"]["body"]
    assert "Best,\nSaman" in body["draft"]["body"]
    assert body["confidence"]["rule_gate_pass"] is True
    assert 0.0 < body["confidence"]["composite"] <= 1.0
    # Slack payload is present, parseable, and within Slack's documented limits.
    slack = body["slack"]
    assert slack is not None
    assert isinstance(slack["text"], str) and 0 < len(slack["text"]) <= 3000
    assert isinstance(slack["blocks"], list) and 0 < len(slack["blocks"]) <= 50
    block_types = {b["type"] for b in slack["blocks"]}
    assert "header" in block_types
    assert "section" in block_types
    assert "context" in block_types
    # The drafted body must appear inside a code fence so literal markdown
    # in the email doesn't get reinterpreted by Slack.
    assert "```" in json.dumps(slack["blocks"])
    # Quoted-thread stripping happened.
    assert "wrote:" not in body["clean_prospect_reply"]


def test_drafter_inbound_idempotent(client, fixture_payload, patches):
    first = client.post(
        "/webhooks/bison/lead-interested",
        headers={"Authorization": f"Bearer {DRAFTER_KEY}"},
        json=fixture_payload,
    )
    assert first.status_code == 200
    second = client.post(
        "/webhooks/bison/lead-interested",
        headers={"Authorization": f"Bearer {DRAFTER_KEY}"},
        json=fixture_payload,
    )
    assert second.status_code == 200
    assert second.json()["skipped_reason"] == "duplicate"
    assert second.json()["draft_id"] == first.json()["draft_id"]


def test_drafter_inbound_rejects_missing_auth(client, fixture_payload):
    # No auth header
    resp = client.post("/webhooks/bison/lead-interested", json=fixture_payload)
    assert resp.status_code == 401


def test_drafter_inbound_rejects_bad_auth(client, fixture_payload):
    resp = client.post(
        "/webhooks/bison/lead-interested",
        headers={"Authorization": "Bearer wrong"},
        json=fixture_payload,
    )
    assert resp.status_code == 401
