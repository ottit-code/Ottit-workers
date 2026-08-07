"""Parse and persist InboxAssure spamcheck.completed webhooks.

Stores into v1.inboxassure_spamchecks + v1.inboxassure_spamcheck_reports.
Distinct from the placement-test poller (inboxassure_placement_results).

Workspace attribution
---------------------
InboxAssure reports include ``workspace_name`` (e.g. "Ottit V2"). That string
is matched (case-insensitive) to ``lib.config.WORKSPACES[].name`` to get the
Ottit ``workspace_id`` (``ws_v1`` / ``ws_v2``). Same registry the dashboard
switcher and Bison/EmailGuard pollers use.

Optional override: query param ``?workspace_id=ws_v2`` on the webhook URL
wins when set (must be a known Ottit workspace id).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException

from lib.config import WORKSPACES, get_workspace
from lib.n8n_payload import unwrap_spamcheck_body
from lib.supabase_client import get_supabase

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_dict(val: Any) -> dict:
    return val if isinstance(val, dict) else {}


def _as_list(val: Any) -> list:
    return val if isinstance(val, list) else []


def parse_spamcheck_payload(raw: Any) -> dict:
    """Unwrap n8n shapes and return the InboxAssure event body."""
    body = unwrap_spamcheck_body(raw)
    spamcheck = body.get("spamcheck")
    if not isinstance(spamcheck, dict) or spamcheck.get("id") is None:
        raise HTTPException(
            status_code=422,
            detail="Invalid InboxAssure payload: missing spamcheck.id",
        )
    return body


def extract_ia_workspace_name(body: dict) -> Optional[str]:
    """First non-empty reports[].workspace_name (InboxAssure label)."""
    for rep in _as_list(body.get("reports")):
        if isinstance(rep, dict) and rep.get("workspace_name"):
            return str(rep["workspace_name"]).strip() or None
    return None


def resolve_workspace_id(
    *,
    ia_workspace_name: Optional[str] = None,
    workspace_id_override: Optional[str] = None,
) -> Optional[str]:
    """Map InboxAssure workspace_name → Ottit workspace_id.

    Priority:
      1. Explicit ``workspace_id`` override (query param) if known
      2. Case-insensitive match of IA name to ``WORKSPACES[].name``
    """
    if workspace_id_override:
        override = workspace_id_override.strip()
        if get_workspace(override):
            return override
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown workspace_id={override!r}. "
                f"Known: {[ws['id'] for ws in WORKSPACES]}"
            ),
        )

    if not ia_workspace_name:
        return None

    needle = ia_workspace_name.strip().lower()
    for ws in WORKSPACES:
        if str(ws.get("name") or "").strip().lower() == needle:
            return ws["id"]
    return None


def _parent_row(
    body: dict,
    *,
    workspace_id: Optional[str] = None,
    workspace_name: Optional[str] = None,
) -> dict:
    sc = _as_dict(body.get("spamcheck"))
    overall = _as_dict(body.get("overall_results"))

    return {
        "ia_spamcheck_id": int(sc["id"]),
        "name": sc.get("name"),
        "status": sc.get("status"),
        "is_domain_based": sc.get("is_domain_based"),
        "subject": sc.get("subject"),
        "email_body": sc.get("body"),
        "conditions": sc.get("conditions"),
        "ia_created_at": sc.get("created_at"),
        "ia_updated_at": sc.get("updated_at"),
        "total_accounts": overall.get("total_accounts"),
        "good_accounts": overall.get("good_accounts"),
        "bad_accounts": overall.get("bad_accounts"),
        "good_accounts_percentage": overall.get("good_accounts_percentage"),
        "bad_accounts_percentage": overall.get("bad_accounts_percentage"),
        "average_google_score": overall.get("average_google_score"),
        "average_outlook_score": overall.get("average_outlook_score"),
        "total_bounced": overall.get("total_bounced"),
        "total_unique_replies": overall.get("total_unique_replies"),
        "total_emails_sent": overall.get("total_emails_sent"),
        "workspace_id": workspace_id,
        "workspace_name": workspace_name,
        "raw": body,
        "updated_at": _now_iso(),
    }


def _report_rows(body: dict, ia_spamcheck_id: int) -> list[dict]:
    rows: list[dict] = []
    for rep in _as_list(body.get("reports")):
        if not isinstance(rep, dict) or not rep.get("id"):
            continue
        email = rep.get("email_account")
        if not email:
            continue
        rows.append(
            {
                "id": str(rep["id"]),
                "ia_spamcheck_id": ia_spamcheck_id,
                "email_account": str(email),
                "google_pro_score": rep.get("google_pro_score"),
                "outlook_pro_score": rep.get("outlook_pro_score"),
                "is_good": rep.get("is_good"),
                "sending_limit": rep.get("sending_limit"),
                "tags_list": rep.get("tags_list"),
                "workspace_name": rep.get("workspace_name"),
                "bounced_count": rep.get("bounced_count"),
                "unique_replied_count": rep.get("unique_replied_count"),
                "emails_sent_count": rep.get("emails_sent_count"),
                "ia_created_at": rep.get("created_at"),
                "updated_at": _now_iso(),
            }
        )
    return rows


def upsert_spamcheck(
    body: dict,
    *,
    workspace_id_override: Optional[str] = None,
) -> dict:
    """Upsert parent + report rows. Returns a summary dict for the HTTP response."""
    ia_workspace_name = extract_ia_workspace_name(body)
    workspace_id = resolve_workspace_id(
        ia_workspace_name=ia_workspace_name,
        workspace_id_override=workspace_id_override,
    )
    if ia_workspace_name and not workspace_id and not workspace_id_override:
        logger.warning(
            "inboxassure_spamcheck unmatched workspace_name=%r "
            "(expected one of %s); storing with workspace_id=null",
            ia_workspace_name,
            [ws["name"] for ws in WORKSPACES],
        )

    parent = _parent_row(
        body,
        workspace_id=workspace_id,
        workspace_name=ia_workspace_name,
    )
    ia_id = parent["ia_spamcheck_id"]
    reports = _report_rows(body, ia_id)
    sb = get_supabase()

    sb.table("inboxassure_spamchecks").upsert(
        parent, on_conflict="ia_spamcheck_id"
    ).execute()

    if reports:
        sb.table("inboxassure_spamcheck_reports").upsert(
            reports, on_conflict="id"
        ).execute()

    logger.info(
        "inboxassure_spamcheck upserted ia_spamcheck_id=%s reports=%s "
        "status=%s workspace_id=%s workspace_name=%s",
        ia_id,
        len(reports),
        parent.get("status"),
        workspace_id,
        ia_workspace_name,
    )
    return {
        "received": True,
        "event": body.get("event") or "spamcheck.completed",
        "ia_spamcheck_id": ia_id,
        "status": parent.get("status"),
        "name": parent.get("name"),
        "reports_upserted": len(reports),
        "workspace_id": workspace_id,
        "workspace_name": ia_workspace_name,
    }


def ingest_spamcheck_webhook(
    raw: Any,
    *,
    workspace_id_override: Optional[str] = None,
) -> dict:
    """Full path: unwrap → validate → resolve workspace → upsert."""
    body = parse_spamcheck_payload(raw)
    return upsert_spamcheck(body, workspace_id_override=workspace_id_override)
