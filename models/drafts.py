"""
Internal data models for the drafter pipeline.

Used between services. The HTTP response shape is defined in
`api/routers/drafter_inbound.py` and intentionally mirrors these models so
n8n sees a stable contract.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class ClaudeDraft(BaseModel):
    """Raw structured output from a Claude call."""

    model_config = ConfigDict(extra="ignore")

    subject: str
    body: str
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    human_review_needed: bool = False
    review_reason: str = ""


class VoiceExample(BaseModel):
    id: int
    content: str
    similarity: float
    metadata: dict = Field(default_factory=dict)


class ConfidenceComponents(BaseModel):
    """Composite confidence breakdown surfaced to n8n.

    Composite is 0 when any rule gate fails — the LLM might have produced
    a fine-looking draft but a hard rule was violated (banned phrase,
    pricing leak, etc.). n8n should branch on `rule_gate_pass` first.
    """

    llm_self_rating: float = Field(ge=0.0, le=1.0, default=0.0)
    rule_gate_pass: bool = False
    rule_gates_failed: List[str] = Field(default_factory=list)
    ensemble_agreement: float = Field(ge=0.0, le=1.0, default=0.0)
    rag_retrieval_quality: float = Field(ge=0.0, le=1.0, default=0.0)
    composite: float = Field(ge=0.0, le=1.0, default=0.0)


class SlackPayload(BaseModel):
    """Slack-ready message body. Pass `text` and `blocks` straight to
    chat.postMessage. n8n doesn't need to know anything about Block Kit."""

    text: str
    blocks: List[Dict[str, Any]] = Field(default_factory=list)


class DraftResult(BaseModel):
    """Final orchestrator output for one Bison reply."""

    draft_id: str
    bison_reply_uuid: str
    subject: str
    body: str
    human_review_needed: bool
    review_reason: str
    confidence: ConfidenceComponents
    rag_examples_used: List[int] = Field(default_factory=list)
    model_primary: Optional[str] = None
    model_ensemble: Optional[str] = None
    duplicate: bool = False
    slack: Optional[SlackPayload] = None
    clean_prospect_reply: str = ""
