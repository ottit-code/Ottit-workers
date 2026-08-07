"""
POST /webhooks/inboxassure/spamcheck-completed — n8n forwards InboxAssure
spamcheck.completed payloads here. We upsert into Supabase and return a
short JSON summary.

Accepts:
  - n8n-wrapped item / array ({ headers, body, … })
  - raw InboxAssure body ({ event, spamcheck, overall_results, reports })

Workspace matching (dashboard filter):
  - Default: reports[].workspace_name ("Ottit V2") → Ottit workspace_id
    (ws_v2) via lib.config.WORKSPACES name match.
  - Optional override: ?workspace_id=ws_v2 on the webhook URL.

Auth: optional. Missing Authorization is allowed.
If Authorization is sent, it must be Bearer DRAFTER_API_KEY
(same optional-auth helper as other n8n forwards).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Security
from pydantic import BaseModel, Field

from api.routers.drafter_deps import require_drafter_key
from lib.inboxassure_spamcheck import ingest_spamcheck_webhook

logger = logging.getLogger(__name__)

router = APIRouter(tags=["inboxassure"])


class SpamcheckIngestResponse(BaseModel):
    received: bool = True
    event: str = "spamcheck.completed"
    ia_spamcheck_id: int
    status: str | None = None
    name: str | None = None
    reports_upserted: int = 0
    workspace_id: str | None = Field(
        default=None,
        description="Ottit workspace id (ws_v1 / ws_v2) resolved from IA name or query override.",
    )
    workspace_name: str | None = Field(
        default=None,
        description="InboxAssure reports[].workspace_name (e.g. Ottit V2), if present.",
    )


@router.post(
    "/webhooks/inboxassure/spamcheck-completed",
    response_model=SpamcheckIngestResponse,
    dependencies=[Security(require_drafter_key)],
)
def spamcheck_completed(
    body: Any = Body(...),
    workspace_id: Optional[str] = Query(
        default=None,
        description=(
            "Optional Ottit workspace id override (ws_v1 / ws_v2). "
            "When omitted, matched from reports[].workspace_name."
        ),
    ),
) -> SpamcheckIngestResponse:
    """Ingest InboxAssure spamcheck.completed (n8n-wrapped or raw)."""
    try:
        summary = ingest_spamcheck_webhook(
            body, workspace_id_override=workspace_id
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("inboxassure_spamcheck.ingest_failed")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to persist spamcheck: {exc}",
        ) from exc
    return SpamcheckIngestResponse(**summary)
