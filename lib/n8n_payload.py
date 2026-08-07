"""Normalize payloads forwarded from n8n (Bison, InboxAssure, etc.).

n8n Webhook → HTTP Request often forwards the whole item (`$json`), which
wraps the real event under `body` / `json`, or as a one-item array.
Accept those shapes so forwards don't 422 on envelope validation.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import HTTPException

from models.bison_payload import BisonEventEnvelope, BisonLeadInterestedData


def _looks_like_envelope(obj: dict) -> bool:
    return "data" in obj and isinstance(obj.get("data"), dict)


def _looks_like_data(obj: dict) -> bool:
    return "reply" in obj and "lead" in obj


def _looks_like_spamcheck(obj: dict) -> bool:
    if isinstance(obj.get("spamcheck"), dict):
        return True
    if obj.get("event") == "spamcheck.completed":
        return True
    return isinstance(obj.get("reports"), list) and "overall_results" in obj


def unwrap_n8n_wrapper(
    raw: Any,
    *,
    is_payload: Callable[[dict], bool] | None = None,
    expected: str = "JSON object",
) -> dict:
    """Peel common n8n wrappers (array / body / json) until a target dict."""
    current: Any = raw

    if isinstance(current, list):
        if not current:
            raise HTTPException(status_code=400, detail="Empty JSON array from n8n")
        current = current[0]

    for _ in range(4):
        if not isinstance(current, dict):
            break

        if is_payload is not None and is_payload(current):
            break

        # Webhook node default: { headers, params, query, body: <event> }
        nested = current.get("body")
        if isinstance(nested, dict):
            current = nested
            continue

        # Item wrapper: { json: <event> }
        nested = current.get("json")
        if isinstance(nested, dict):
            current = nested
            continue

        break

    if not isinstance(current, dict):
        raise HTTPException(
            status_code=400,
            detail=f"Expected a {expected}",
        )
    return current


def unwrap_n8n_body(raw: Any) -> dict:
    """Peel common n8n wrappers until we reach a Bison-shaped dict."""
    return unwrap_n8n_wrapper(
        raw,
        is_payload=lambda obj: _looks_like_envelope(obj) or _looks_like_data(obj),
        expected="JSON object (Bison LEAD_INTERESTED envelope)",
    )


def unwrap_spamcheck_body(raw: Any) -> dict:
    """Peel n8n wrappers until InboxAssure spamcheck.completed body."""
    return unwrap_n8n_wrapper(
        raw,
        is_payload=_looks_like_spamcheck,
        expected="JSON object (InboxAssure spamcheck.completed)",
    )


def parse_bison_envelope(raw: Any) -> BisonEventEnvelope:
    """Unwrap n8n shapes and validate as BisonEventEnvelope."""
    obj = unwrap_n8n_body(raw)

    # Allow forwarding just the `data` object without the outer envelope.
    if _looks_like_data(obj) and not _looks_like_envelope(obj):
        obj = {"event": {"type": "LEAD_INTERESTED"}, "data": obj}

    try:
        return BisonEventEnvelope.model_validate(obj)
    except Exception as exc:
        # Surface a clean 422 rather than a raw pydantic dump when nested wrong.
        raise HTTPException(
            status_code=422,
            detail=f"Invalid Bison LEAD_INTERESTED payload: {exc}",
        ) from exc


def parse_bison_data(raw: Any) -> BisonLeadInterestedData:
    return parse_bison_envelope(raw).data
