"""Tests for InboxAssure spamcheck.completed ingest."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from lib.inboxassure_spamcheck import (
    parse_spamcheck_payload,
    resolve_workspace_id,
    upsert_spamcheck,
)
from lib.n8n_payload import unwrap_spamcheck_body

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "spamcheck_completed.json").read_text()
)
DRAFTER_KEY = "test-drafter-key"


def test_unwrap_raw_spamcheck():
    body = unwrap_spamcheck_body(FIXTURE)
    assert body["spamcheck"]["id"] == 1179
    assert body["event"] == "spamcheck.completed"


def test_unwrap_n8n_array_wrapper():
    wrapped = [
        {
            "headers": {"user-agent": "InboxAssure-Webhook/1.0"},
            "params": {},
            "query": {},
            "body": FIXTURE,
            "webhookUrl": "https://n8n.example/webhook/ia",
            "executionMode": "production",
        }
    ]
    body = unwrap_spamcheck_body(wrapped)
    assert body["spamcheck"]["id"] == 1179
    assert len(body["reports"]) == 2


def test_parse_maps_email_body():
    body = parse_spamcheck_payload({"body": FIXTURE})
    assert "Confirming tomorrow" in body["spamcheck"]["body"]


def test_parse_rejects_missing_spamcheck():
    with pytest.raises(HTTPException) as ei:
        parse_spamcheck_payload({"event": "spamcheck.completed", "reports": []})
    assert ei.value.status_code == 422


def test_resolve_workspace_from_ia_name():
    assert resolve_workspace_id(ia_workspace_name="Ottit V2") == "ws_v2"
    assert resolve_workspace_id(ia_workspace_name="ottit v1") == "ws_v1"
    assert resolve_workspace_id(ia_workspace_name="Unknown Org") is None


def test_resolve_workspace_override():
    assert (
        resolve_workspace_id(
            ia_workspace_name="Ottit V2",
            workspace_id_override="ws_v1",
        )
        == "ws_v1"
    )
    with pytest.raises(HTTPException) as ei:
        resolve_workspace_id(workspace_id_override="ws_nope")
    assert ei.value.status_code == 422


def test_upsert_writes_parent_and_reports():
    parent_calls = []
    report_calls = []

    class _Table:
        def __init__(self, name):
            self.name = name

        def upsert(self, rows, on_conflict=None):
            if self.name == "inboxassure_spamchecks":
                parent_calls.append((rows, on_conflict))
            else:
                report_calls.append((rows, on_conflict))
            return self

        def execute(self):
            return MagicMock(data=[])

    sb = MagicMock()
    sb.table.side_effect = lambda name: _Table(name)

    with patch("lib.inboxassure_spamcheck.get_supabase", return_value=sb):
        summary = upsert_spamcheck(FIXTURE)

    assert summary["ia_spamcheck_id"] == 1179
    assert summary["reports_upserted"] == 2
    assert summary["workspace_name"] == "Ottit V2"
    assert summary["workspace_id"] == "ws_v2"
    assert parent_calls[0][0]["workspace_id"] == "ws_v2"
    assert parent_calls[0][0]["email_body"] == FIXTURE["spamcheck"]["body"]
    assert parent_calls[0][0]["total_accounts"] == FIXTURE["overall_results"]["total_accounts"]
    assert parent_calls[0][1] == "ia_spamcheck_id"
    assert report_calls[0][1] == "id"
    assert report_calls[0][0][0]["email_account"] == "davis@ottitinsights.com"


@pytest.fixture
def client():
    from api.main import app

    return TestClient(app)


def test_endpoint_accepts_n8n_wrapper_without_auth(client):
    wrapped = [
        {
            "headers": {},
            "params": {},
            "query": {},
            "body": FIXTURE,
            "webhookUrl": "https://n8n.example/webhook/ia",
            "executionMode": "production",
        }
    ]
    with patch(
        "api.routers.inboxassure_spamcheck.ingest_spamcheck_webhook",
        return_value={
            "received": True,
            "event": "spamcheck.completed",
            "ia_spamcheck_id": 1179,
            "status": "completed",
            "name": FIXTURE["spamcheck"]["name"],
            "reports_upserted": 2,
            "workspace_id": "ws_v2",
            "workspace_name": "Ottit V2",
        },
    ) as ingest:
        resp = client.post(
            "/webhooks/inboxassure/spamcheck-completed",
            json=wrapped,
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ia_spamcheck_id"] == 1179
    assert data["reports_upserted"] == 2
    assert data["workspace_id"] == "ws_v2"
    ingest.assert_called_once()


def test_endpoint_passes_workspace_id_query(client):
    with patch(
        "api.routers.inboxassure_spamcheck.ingest_spamcheck_webhook",
        return_value={
            "received": True,
            "event": "spamcheck.completed",
            "ia_spamcheck_id": 1179,
            "status": "completed",
            "name": "x",
            "reports_upserted": 0,
            "workspace_id": "ws_v1",
            "workspace_name": "Ottit V2",
        },
    ) as ingest:
        resp = client.post(
            "/webhooks/inboxassure/spamcheck-completed?workspace_id=ws_v1",
            json=FIXTURE,
        )
    assert resp.status_code == 200, resp.text
    ingest.assert_called_once()
    assert ingest.call_args.kwargs.get("workspace_id_override") == "ws_v1"


def test_endpoint_rejects_bad_auth(client):
    resp = client.post(
        "/webhooks/inboxassure/spamcheck-completed",
        headers={"Authorization": "Bearer wrong"},
        json=FIXTURE,
    )
    assert resp.status_code == 401


def test_endpoint_accepts_valid_auth(client):
    with patch(
        "api.routers.inboxassure_spamcheck.ingest_spamcheck_webhook",
        return_value={
            "received": True,
            "event": "spamcheck.completed",
            "ia_spamcheck_id": 1179,
            "status": "completed",
            "name": "x",
            "reports_upserted": 0,
            "workspace_id": None,
            "workspace_name": None,
        },
    ):
        resp = client.post(
            "/webhooks/inboxassure/spamcheck-completed",
            headers={"Authorization": f"Bearer {DRAFTER_KEY}"},
            json=FIXTURE,
        )
    assert resp.status_code == 200, resp.text


def test_list_spamchecks_scopes_workspace():
    from lib.inboxassure_spamcheck import list_spamchecks

    class _Query:
        def __init__(self):
            self.filters = {}

        def select(self, *_a, **_k):
            return self

        def order(self, *_a, **_k):
            return self

        def limit(self, *_a, **_k):
            return self

        def eq(self, key, value):
            self.filters[key] = value
            return self

        def execute(self):
            return MagicMock(data=[{"ia_spamcheck_id": 1, "workspace_id": "ws_v2"}])

    q = _Query()
    sb = MagicMock()
    sb.table.return_value = q

    with patch("lib.inboxassure_spamcheck.get_supabase", return_value=sb):
        rows = list_spamchecks(workspace_id="ws_v2", limit=10)

    assert rows[0]["ia_spamcheck_id"] == 1
    assert q.filters["workspace_id"] == "ws_v2"


def test_get_spamcheck_includes_reports():
    from lib.inboxassure_spamcheck import get_spamcheck

    parent = {
        "ia_spamcheck_id": 1179,
        "name": "Ottit: SM-GOOG-SET3-0609: MWF Check",
        "workspace_id": "ws_v2",
    }
    reports = [
        {"id": "a", "email_account": "a@x.com", "is_good": True},
        {"id": "b", "email_account": "b@x.com", "is_good": False},
    ]

    class _ParentQ:
        def select(self, *_a, **_k):
            return self

        def eq(self, *_a, **_k):
            return self

        def limit(self, *_a, **_k):
            return self

        def execute(self):
            return MagicMock(data=[parent])

    class _ReportQ:
        def select(self, *_a, **_k):
            return self

        def eq(self, *_a, **_k):
            return self

        def order(self, *_a, **_k):
            return self

        def execute(self):
            return MagicMock(data=reports)

    sb = MagicMock()
    sb.table.side_effect = lambda name: (
        _ParentQ() if name == "inboxassure_spamchecks" else _ReportQ()
    )

    with patch("lib.inboxassure_spamcheck.get_supabase", return_value=sb):
        run = get_spamcheck(1179)

    assert run is not None
    assert run["ia_spamcheck_id"] == 1179
    assert len(run["reports"]) == 2


def test_list_spamchecks_endpoint(client):
    with patch(
        "lib.inboxassure_spamcheck.list_spamchecks",
        return_value=[
            {
                "ia_spamcheck_id": 1179,
                "name": "Ottit: SM-GOOG-SET3-0609: MWF Check",
                "status": "completed",
                "workspace_id": "ws_v2",
            }
        ],
    ):
        resp = client.get(
            "/deliverability/inboxassure/spamchecks?workspace_id=ws_v2",
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["spamchecks"][0]["ia_spamcheck_id"] == 1179


def test_get_spamcheck_endpoint_workspace_mismatch(client):
    with patch(
        "lib.inboxassure_spamcheck.get_spamcheck",
        return_value={
            "ia_spamcheck_id": 1179,
            "workspace_id": "ws_v2",
            "reports": [],
        },
    ):
        resp = client.get(
            "/deliverability/inboxassure/spamchecks/1179?workspace_id=ws_v1",
        )
    assert resp.status_code == 404
