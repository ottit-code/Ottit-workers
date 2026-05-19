"""
Drafter orchestrator. Wires together the five-layer prompt, the two Claude
calls, the confidence scoring, the persistence layer, and the audit log.

Sync entrypoint: `run(payload)` returns a DraftResult and is what the
inbound router calls inside the request lifecycle. Total latency budget
is bounded by DRAFT_TIMEOUT_SECONDS but typical runs land in 5–15 s.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from lib import (
    anthropic_client,
    audit,
    confidence,
    config,
    lead_enricher,
    openai_embed,
    rag,
    reply_drafts_dao,
    reply_parser,
    slack_format,
)
from lib.voice_loader import get_voice_loader
from models.bison_payload import BisonLeadInterestedData
from models.drafts import DraftResult, ConfidenceComponents, SlackPayload
from prompts import templates

logger = logging.getLogger(__name__)


class DrafterError(RuntimeError):
    """Raised by run() on fatal pipeline failure (logged + 500-back to n8n)."""


def run(payload: BisonLeadInterestedData) -> DraftResult:
    """Generate a draft for a Bison LEAD_INTERESTED event.

    Idempotent: if another worker already produced a draft for this
    `bison_reply_uuid`, returns the stored draft with `duplicate=True`.
    """
    uuid = payload.reply.uuid
    started = time.monotonic()
    logger.info("drafter.run.start bison_reply_uuid=%s lead_id=%s", uuid, payload.lead.id)

    claimed = reply_drafts_dao.claim(payload)
    if claimed is None:
        existing = reply_drafts_dao.get_by_uuid(uuid)
        if existing is None:
            raise DrafterError(f"Idempotency conflict but no existing row for uuid={uuid}")
        logger.info("drafter.run.duplicate bison_reply_uuid=%s", uuid)
        return _result_from_row(existing, payload=payload, duplicate=True)

    draft_id = claimed["id"]

    try:
        result = _generate(draft_id, payload)
    except Exception:
        # Pipeline failed — roll back the placeholder row so the next webhook
        # retry isn't a "duplicate" no-op.
        reply_drafts_dao.delete(draft_id)
        logger.exception("drafter.run.failed bison_reply_uuid=%s draft_id=%s", uuid, draft_id)
        raise

    duration_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "drafter.run.complete bison_reply_uuid=%s draft_id=%s composite=%.3f duration_ms=%d",
        uuid, draft_id, result.confidence.composite, duration_ms,
    )
    return result


# --- internal pipeline ------------------------------------------------------

def _generate(draft_id: str, payload: BisonLeadInterestedData) -> DraftResult:
    clean_reply = reply_parser.strip_quoted_thread(payload.reply.text_body)

    lead_snapshot = lead_enricher.fetch_latest_snapshot(payload.lead.id)

    # Embed prospect reply once; reused by RAG retrieval and downstream stats.
    prospect_embedding = openai_embed.embed(clean_reply)
    examples = rag.retrieve_voice_examples(prospect_embedding, k=config.RAG_TOP_K)

    instructions_md, skill_md = get_voice_loader().get()
    system_prompt = templates.build_system_prompt(instructions_md, skill_md)
    user_message = templates.build_user_message(
        payload=payload,
        clean_prospect_reply=clean_reply,
        voice_examples=examples,
        lead_snapshot=lead_snapshot,
    )

    primary = anthropic_client.call_primary(system_prompt, user_message)
    ensemble = _try_ensemble(system_prompt, user_message)

    failed_gates = confidence.evaluate_rule_gates(primary.body)
    ensemble_agreement = _ensemble_agreement(primary.body, ensemble.body if ensemble else "")
    rag_quality = confidence.rag_retrieval_quality(examples)

    composite, components = confidence.composite_score(
        draft=primary,
        failed_gates=failed_gates,
        ensemble_agreement=ensemble_agreement,
        rag_quality=rag_quality,
    )

    human_review_needed = primary.human_review_needed or not components.rule_gate_pass
    review_reason = primary.review_reason
    if not components.rule_gate_pass and not review_reason:
        review_reason = "rule_gates_failed: " + ", ".join(components.rule_gates_failed)

    rag_ids = [e.id for e in examples]
    reply_drafts_dao.finalize(
        draft_id=draft_id,
        drafted_subject=primary.subject,
        drafted_body=primary.body,
        confidence_composite=composite,
        confidence_components=components.model_dump(),
        human_review_needed=human_review_needed,
        review_reason=review_reason,
        rule_gates_failed=components.rule_gates_failed,
        rag_examples_used=rag_ids,
        model_primary=config.CLAUDE_MODEL_PRIMARY,
        model_ensemble=config.CLAUDE_MODEL_ENSEMBLE if ensemble else "",
    )

    audit.log(
        action="draft.created",
        target_type="reply",
        target_id=str(payload.reply.id),
        target_email=payload.reply.from_email_address,
        new_value={"subject": primary.subject, "body": primary.body},
        metadata={
            "draft_id": draft_id,
            "bison_reply_uuid": payload.reply.uuid,
            "confidence": components.model_dump(),
            "rag_examples_used": rag_ids,
            "model_primary": config.CLAUDE_MODEL_PRIMARY,
            "model_ensemble": config.CLAUDE_MODEL_ENSEMBLE if ensemble else None,
        },
    )

    _upsert_review_state(payload.reply.id)

    slack_payload = SlackPayload(**slack_format.build_slack_payload(
        draft_id=draft_id,
        payload=payload,
        clean_prospect_reply=clean_reply,
        subject=primary.subject,
        body=primary.body,
        confidence=components,
        review_reason=review_reason,
        rag_examples_used=rag_ids,
        model_primary=config.CLAUDE_MODEL_PRIMARY,
    ))

    return DraftResult(
        draft_id=draft_id,
        bison_reply_uuid=payload.reply.uuid,
        subject=primary.subject,
        body=primary.body,
        human_review_needed=human_review_needed,
        review_reason=review_reason,
        confidence=components,
        rag_examples_used=rag_ids,
        model_primary=config.CLAUDE_MODEL_PRIMARY,
        model_ensemble=config.CLAUDE_MODEL_ENSEMBLE if ensemble else None,
        duplicate=False,
        slack=slack_payload,
        clean_prospect_reply=clean_reply,
    )


def _try_ensemble(system: str, user: str):
    """Ensemble call is best-effort — its only purpose is the agreement signal."""
    try:
        return anthropic_client.call_ensemble(system, user)
    except Exception as exc:
        logger.warning("drafter.ensemble_failed: %s", exc)
        return None


def _ensemble_agreement(primary_body: str, ensemble_body: str) -> float:
    if not ensemble_body:
        return 0.0
    try:
        a = openai_embed.embed(primary_body)
        b = openai_embed.embed(ensemble_body)
    except Exception as exc:
        logger.warning("drafter.ensemble_embedding_failed: %s", exc)
        return 0.0
    cosine = openai_embed.cosine_similarity(a, b)
    # Map [-1, 1] → [0, 1] so it composes with the other 0-1 components.
    return max(0.0, min(1.0, (cosine + 1.0) / 2.0))


def _upsert_review_state(reply_id: int) -> None:
    from lib.supabase_client import get_supabase
    from datetime import datetime, timezone

    try:
        get_supabase().table("reply_review_state").upsert(
            {
                "reply_id": str(reply_id),
                "review_state": "pending",
                "classification": "interested",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="reply_id",
        ).execute()
    except Exception as exc:
        logger.warning("drafter.review_state_upsert_failed: %s", exc)


def _result_from_row(row: dict, *, payload: BisonLeadInterestedData, duplicate: bool) -> DraftResult:
    cc = row.get("confidence_components") or {}
    components = ConfidenceComponents(
        llm_self_rating=float(cc.get("llm_self_rating") or 0.0),
        rule_gate_pass=bool(cc.get("rule_gate_pass", False)),
        rule_gates_failed=list(cc.get("rule_gates_failed") or []),
        ensemble_agreement=float(cc.get("ensemble_agreement") or 0.0),
        rag_retrieval_quality=float(cc.get("rag_retrieval_quality") or 0.0),
        composite=float(row.get("confidence_composite") or 0.0),
    )
    subject = row.get("drafted_subject") or ""
    body = row.get("drafted_body") or ""
    clean_reply = reply_parser.strip_quoted_thread(payload.reply.text_body)
    rag_ids = list(row.get("rag_examples_used") or [])
    slack_payload = SlackPayload(**slack_format.build_slack_payload(
        draft_id=row["id"],
        payload=payload,
        clean_prospect_reply=clean_reply,
        subject=subject,
        body=body,
        confidence=components,
        review_reason=row.get("review_reason") or "",
        rag_examples_used=rag_ids,
        model_primary=row.get("model_primary"),
    ))
    return DraftResult(
        draft_id=row["id"],
        bison_reply_uuid=row["bison_reply_uuid"],
        subject=subject,
        body=body,
        human_review_needed=bool(row.get("human_review_needed")),
        review_reason=row.get("review_reason") or "",
        confidence=components,
        rag_examples_used=rag_ids,
        model_primary=row.get("model_primary"),
        model_ensemble=row.get("model_ensemble"),
        duplicate=duplicate,
        slack=slack_payload,
        clean_prospect_reply=clean_reply,
    )
