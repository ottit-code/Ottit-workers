"""
Tests for lib/slack_format.py.

These assert the contract that n8n consumes: a `text` string and a `blocks`
array that satisfies Slack's documented Block Kit limits so it can be passed
straight to chat.postMessage.

Slack limits we enforce (per https://docs.slack.dev/block-kit/):
  - <= 50 blocks total per message
  - section block text <= 3000 chars
  - header block text  <= 150 chars
"""
from __future__ import annotations

import json

from lib import slack_format
from models.bison_payload import (
    BisonCampaign,
    BisonLead,
    BisonLeadInterestedData,
    BisonReply,
    BisonScheduledEmail,
    BisonSenderEmail,
)
from models.drafts import ConfidenceComponents


def _payload(name="James", company="Acme Healthcare") -> BisonLeadInterestedData:
    return BisonLeadInterestedData(
        reply=BisonReply(
            id=1,
            uuid="u-1",
            text_body="hi",
            email_subject="Re: x",
            from_email_address="james@acme.io",
        ),
        lead=BisonLead(id=1, email="james@acme.io", first_name=name, last_name="Holt", company=company),
        campaign=BisonCampaign(id=42, name="Campaign Q2"),
        scheduled_email=BisonScheduledEmail(sequence_step_order=3),
        sender_email=BisonSenderEmail(id=99, email="saman@send.ottit.com"),
    )


def _components(composite=0.82) -> ConfidenceComponents:
    return ConfidenceComponents(
        llm_self_rating=0.85,
        rule_gate_pass=True,
        rule_gates_failed=[],
        ensemble_agreement=0.80,
        rag_retrieval_quality=0.70,
        composite=composite,
    )


def test_escape_mrkdwn_escapes_html_entities():
    assert slack_format.escape_mrkdwn("a < b > c & d") == "a &lt; b &gt; c &amp; d"


def test_escape_mrkdwn_handles_empty():
    assert slack_format.escape_mrkdwn("") == ""
    assert slack_format.escape_mrkdwn(None) == ""  # type: ignore[arg-type]


def test_build_slack_payload_returns_text_and_blocks():
    p = slack_format.build_slack_payload(
        draft_id="d-1",
        payload=_payload(),
        clean_prospect_reply="Hey, this is interesting — what do you charge?",
        subject="Re: monthly close drag?",
        body="Hi James,\n\nThanks for the reply. Best,\nSaman",
        confidence=_components(),
        rag_examples_used=[1, 2, 3],
        model_primary="claude-opus-4-7",
    )
    assert isinstance(p["text"], str) and p["text"]
    assert isinstance(p["blocks"], list) and len(p["blocks"]) > 0


def test_blocks_respect_slack_limits():
    p = slack_format.build_slack_payload(
        draft_id="d-1",
        payload=_payload(),
        clean_prospect_reply="x" * 5000,        # oversize to exercise truncation
        subject="Re: " + ("subj " * 200),
        body=("Hi James,\n\n" + "Lorem ipsum " * 800 + "\n\nBest,\nSaman"),
        confidence=_components(),
        rag_examples_used=list(range(20)),
        model_primary="claude-opus-4-7",
    )

    # <= 50 blocks
    assert len(p["blocks"]) <= 50

    # Every block has a recognised type
    valid_types = {"header", "section", "divider", "context", "actions"}
    for block in p["blocks"]:
        assert block["type"] in valid_types, block

    # header text <= 150 chars
    headers = [b for b in p["blocks"] if b["type"] == "header"]
    for h in headers:
        assert len(h["text"]["text"]) <= 150

    # section text <= 3000 chars (and fields text <= 2000 per Slack)
    for sec in [b for b in p["blocks"] if b["type"] == "section"]:
        if "text" in sec:
            assert len(sec["text"]["text"]) <= 3000
        if "fields" in sec:
            for f in sec["fields"]:
                assert len(f["text"]) <= 2000

    # text field <= 3000 (and present so chat.postMessage notifications work)
    assert 0 < len(p["text"]) <= 3000


def test_blocks_serialize_to_json():
    """If json.dumps fails, n8n can't post it."""
    p = slack_format.build_slack_payload(
        draft_id="d-1",
        payload=_payload(),
        clean_prospect_reply="hi",
        subject="Re: x",
        body="Hi James,\n\nshort body.\n\nBest,\nSaman",
        confidence=_components(),
        rag_examples_used=[7],
        model_primary="claude-opus-4-7",
    )
    raw = json.dumps(p)
    assert "Hi James" in raw
    assert "draft_id" in raw  # context block


def test_blocks_include_human_review_section_when_needed():
    p = slack_format.build_slack_payload(
        draft_id="d-1",
        payload=_payload(),
        clean_prospect_reply="hi",
        subject="Re: x",
        body="Hi James,\n\nshort body.\n\nBest,\nSaman",
        confidence=_components(composite=0.0),
        review_reason="rule_gates_failed: pricing_leak",
        rag_examples_used=[],
        model_primary="claude-opus-4-7",
    )
    raw = json.dumps(p)
    assert "Human review suggested" in raw
    assert "pricing_leak" in raw


def test_blocks_omit_review_section_when_not_needed():
    p = slack_format.build_slack_payload(
        draft_id="d-1",
        payload=_payload(),
        clean_prospect_reply="hi",
        subject="Re: x",
        body="Hi James,\n\nshort body.\n\nBest,\nSaman",
        confidence=_components(),
        review_reason="",
        rag_examples_used=[],
        model_primary="claude-opus-4-7",
    )
    raw = json.dumps(p)
    assert "Human review suggested" not in raw


def test_confidence_emoji_colour_thresholds():
    assert slack_format._confidence_emoji(0.95) == ":large_green_circle:"
    assert slack_format._confidence_emoji(0.65) == ":large_yellow_circle:"
    assert slack_format._confidence_emoji(0.10) == ":red_circle:"


def test_lead_name_fallback_to_email_when_missing():
    p = slack_format.build_slack_payload(
        draft_id="d-1",
        payload=_payload(name="", company=""),
        clean_prospect_reply="hi",
        subject="Re: x",
        body="Hi James,\n\nshort body.\n\nBest,\nSaman",
        confidence=_components(),
        rag_examples_used=[],
    )
    # James Holt → Holt because first_name="", or fall back to email entirely
    raw = json.dumps(p)
    assert "Holt" in raw or "james@acme.io" in raw


def test_body_contains_code_fence_so_email_isnt_mrkdwn_formatted():
    """The drafted email might include `*` or `_` literals — they must not
    become Slack bold/italic. We wrap the body in triple-backticks."""
    body = "Hi James,\n\nI saw your *new* product launch. Best,\nSaman"
    p = slack_format.build_slack_payload(
        draft_id="d-1",
        payload=_payload(),
        clean_prospect_reply="hi",
        subject="Re: x",
        body=body,
        confidence=_components(),
        rag_examples_used=[],
    )
    raw = json.dumps(p)
    assert "```" in raw, "Email body should be wrapped in code fences"
