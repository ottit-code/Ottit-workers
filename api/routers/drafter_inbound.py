"""
POST /webhooks/bison/lead-interested — n8n forwards the raw Bison
LEAD_INTERESTED payload here. We draft synchronously and return the draft
in the HTTP response.

Typical latency 5–15 s. Hard cap is `DRAFT_TIMEOUT_SECONDS`.

Auth: Bearer DRAFTER_API_KEY.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Security
from pydantic import BaseModel, Field

from api.routers.drafter_deps import require_drafter_key
from lib import drafter
from models.bison_payload import BisonEventEnvelope
from models.drafts import ConfidenceComponents, SlackPayload

logger = logging.getLogger(__name__)

router = APIRouter(tags=["drafter"])


class DraftBody(BaseModel):
    subject: str
    body: str
    human_review_needed: bool
    review_reason: str = ""


class ContextBlock(BaseModel):
    rag_examples_used: list[int] = Field(default_factory=list)
    model_primary: Optional[str] = None
    model_ensemble: Optional[str] = None


class DraftResponse(BaseModel):
    received: bool = True
    bison_reply_uuid: str
    draft_id: str
    skipped_reason: Optional[str] = None
    draft: DraftBody
    confidence: ConfidenceComponents
    context: ContextBlock
    slack: Optional[SlackPayload] = Field(
        default=None,
        description="Pass slack.text and slack.blocks straight to chat.postMessage.",
    )
    clean_prospect_reply: str = Field(
        default="",
        description="The prospect's reply text with quoted-thread content stripped.",
    )


@router.post(
    "/webhooks/bison/lead-interested",
    response_model=DraftResponse,
    dependencies=[Security(require_drafter_key)],
)
def lead_interested(envelope: BisonEventEnvelope) -> DraftResponse:
    payload = envelope.data
    try:
        result = drafter.run(payload)
    except drafter.DrafterError as exc:
        logger.error("drafter_inbound.drafter_error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:
        logger.exception("drafter_inbound.unexpected_error")
        raise HTTPException(status_code=500, detail=f"Drafter pipeline failed: {exc}")

    return DraftResponse(
        bison_reply_uuid=result.bison_reply_uuid,
        draft_id=result.draft_id,
        skipped_reason="duplicate" if result.duplicate else None,
        draft=DraftBody(
            subject=result.subject,
            body=result.body,
            human_review_needed=result.human_review_needed,
            review_reason=result.review_reason,
        ),
        confidence=result.confidence,
        context=ContextBlock(
            rag_examples_used=result.rag_examples_used,
            model_primary=result.model_primary,
            model_ensemble=result.model_ensemble,
        ),
        slack=result.slack,
        clean_prospect_reply=result.clean_prospect_reply,
    )
