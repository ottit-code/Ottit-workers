"""Unit tests for n8n → Bison envelope unwrapping."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from lib.n8n_payload import parse_bison_envelope, unwrap_n8n_body, unwrap_spamcheck_body

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "lead_interested.json").read_text()
)


def test_raw_bison_envelope():
    env = parse_bison_envelope(FIXTURE)
    assert env.data.reply.uuid == "31a04c41-1bf6-4dab-a0a8-bb2a90b89195"
    assert env.data.lead.email == "james@acmehealth.io"


def test_n8n_webhook_body_wrapper():
    wrapped = {
        "headers": {"content-type": "application/json"},
        "params": {},
        "query": {},
        "body": FIXTURE,
        "webhookUrl": "https://n8n.example/webhook/bison",
    }
    env = parse_bison_envelope(wrapped)
    assert env.data.lead.first_name == "James"


def test_n8n_json_item_wrapper():
    env = parse_bison_envelope({"json": FIXTURE})
    assert env.data.campaign.id == 332


def test_n8n_array_of_items():
    env = parse_bison_envelope([{"json": FIXTURE}])
    assert env.data.sender_email.email == "saman@send.ottit.com"


def test_data_only_without_envelope():
    env = parse_bison_envelope(FIXTURE["data"])
    assert env.event_type == "LEAD_INTERESTED"
    assert env.data.reply.id == 91423


def test_unwrap_rejects_empty_array():
    with pytest.raises(HTTPException) as ei:
        unwrap_n8n_body([])
    assert ei.value.status_code == 400


def test_invalid_payload_422():
    with pytest.raises(HTTPException) as ei:
        parse_bison_envelope({"event": "LEAD_INTERESTED", "data": {"nope": True}})
    assert ei.value.status_code == 422


def test_unwrap_spamcheck_from_n8n_item():
    ia = {
        "event": "spamcheck.completed",
        "spamcheck": {"id": 1173, "body": "hi", "status": "completed"},
        "overall_results": {"total_accounts": 1},
        "reports": [],
    }
    body = unwrap_spamcheck_body(
        [{"headers": {}, "params": {}, "query": {}, "body": ia}]
    )
    assert body["spamcheck"]["id"] == 1173

