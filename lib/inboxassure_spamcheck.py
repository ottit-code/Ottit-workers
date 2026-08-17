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
import re
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


# ---------------------------------------------------------------------------
# Keep-list — only these six InboxAssure tests are shown
# ---------------------------------------------------------------------------
# Ops keep-list (Aug 2026). Names in IA look like:
#   EB: Ottit: CI-DED-Set6-05/18: MWF
#   EB: Ottit: SM-GOOG-Set3-08/12: MWF
# Slash in the date stamp is optional (05/18 vs 0518). Set1/Set2 dates
# may appear as 06/0, 06/09, or 0609.

_ALLOWED_IA_TEST_PATTERNS = (
    re.compile(r"ci[-_]?ded[-_]?set6[-_/]?05/?18", re.I),
    re.compile(r"ci[-_]?ded[-_]?set5[-_/]?05/?18", re.I),
    re.compile(r"ci[-_]?ded[-_]?set4[-_/]?05/?18", re.I),
    re.compile(r"sm[-_]?goog[-_]?set3[-_/]?08/?12", re.I),
    re.compile(r"sm[-_]?goog[-_]?set2[-_/]?06/?0(?:9)?(?!\d)", re.I),
    re.compile(r"sm[-_]?goog[-_]?set1[-_/]?06/?0(?:9)?(?!\d)", re.I),
)

_IA_LIST_FETCH_CAP = 200


def is_kept_ia_spamcheck_name(name: Optional[str]) -> bool:
    """True only for the six current InboxAssure seed-set tests."""
    if not name or not str(name).strip():
        return False
    return any(p.search(str(name)) for p in _ALLOWED_IA_TEST_PATTERNS)


# ---------------------------------------------------------------------------
# Read helpers (dashboard)
# ---------------------------------------------------------------------------

_SPAMCHECK_LIST_COLS = (
    "ia_spamcheck_id,name,status,subject,conditions,"
    "ia_created_at,ia_updated_at,"
    "total_accounts,good_accounts,bad_accounts,"
    "good_accounts_percentage,bad_accounts_percentage,"
    "average_google_score,average_outlook_score,"
    "total_bounced,total_unique_replies,total_emails_sent,"
    "workspace_id,workspace_name,received_at,updated_at"
)

_SPAMCHECK_REPORT_COLS = (
    "id,ia_spamcheck_id,email_account,"
    "google_pro_score,outlook_pro_score,is_good,sending_limit,"
    "tags_list,workspace_name,bounced_count,unique_replied_count,"
    "emails_sent_count,ia_created_at"
)


def _normalize_workspace_filter(workspace_id: Optional[str]) -> Optional[str]:
    """None/'all' → no filter; otherwise return the workspace id."""
    if workspace_id and workspace_id != "all":
        return workspace_id
    return None


def list_spamchecks(
    *,
    workspace_id: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """Latest kept spamcheck runs, newest first. Workspace-scoped when provided.

    Extra InboxAssure tests are dropped; only the six seed-set names remain.
    """
    if limit < 1:
        limit = 1
    if limit > 200:
        limit = 200

    ws = _normalize_workspace_filter(workspace_id)
    query = (
        get_supabase()
        .table("inboxassure_spamchecks")
        .select(_SPAMCHECK_LIST_COLS)
        .order("ia_updated_at", desc=True)
        .limit(_IA_LIST_FETCH_CAP)
    )
    if ws:
        query = query.eq("workspace_id", ws)
    rows = query.execute().data or []
    kept = [r for r in rows if is_kept_ia_spamcheck_name(r.get("name"))]
    return kept[:limit]


def get_spamcheck(ia_spamcheck_id: int) -> Optional[dict]:
    """One spamcheck run plus its per-account reports, or None if missing."""
    sb = get_supabase()
    parent_rows = (
        sb.table("inboxassure_spamchecks")
        .select(_SPAMCHECK_LIST_COLS)
        .eq("ia_spamcheck_id", ia_spamcheck_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not parent_rows:
        return None

    run = dict(parent_rows[0])
    if not is_kept_ia_spamcheck_name(run.get("name")):
        return None

    reports = (
        sb.table("inboxassure_spamcheck_reports")
        .select(_SPAMCHECK_REPORT_COLS)
        .eq("ia_spamcheck_id", ia_spamcheck_id)
        .order("email_account")
        .execute()
        .data
        or []
    )
    run["reports"] = reports
    return run
