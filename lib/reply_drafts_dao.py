"""
Data-access for the `reply_drafts` table.

Idempotency is enforced at the database level by the unique constraint on
`bison_reply_uuid` (migration 006). `claim()` performs a race-safe insert:
the first caller for a given uuid wins; everyone else gets `None` and the
existing row.

This module is intentionally thin — no business logic, just DB I/O.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence
from uuid import uuid4

from lib.supabase_client import get_supabase
from models.bison_payload import BisonLeadInterestedData

logger = logging.getLogger(__name__)

_TABLE = "reply_drafts"


def claim(payload: BisonLeadInterestedData) -> Optional[Dict[str, Any]]:
    """Race-safe insert.

    Returns the new row dict (status='draft_pending') if this caller won;
    returns None if another worker already inserted for this uuid.
    """
    row = {
        "id": str(uuid4()),
        "reply_id": str(payload.reply.id),
        "bison_reply_uuid": payload.reply.uuid,
        # Required-NOT-NULL columns get placeholder values until the drafter fills them.
        "drafted_subject": "",
        "drafted_body": "",
        "sender_email": payload.sender_email.email,
        "sender_email_id": payload.sender_email.id,
        "lead_email": payload.reply.from_email_address,
        "lead_id": payload.lead.id,
        "bison_reply_id": payload.reply.id,
        "reply_message_id": payload.reply.raw_message_id,
        "original_message_id": payload.scheduled_email.raw_message_id if payload.scheduled_email else None,
    }
    try:
        # PostgREST exposes ON CONFLICT via .upsert(..., ignore_duplicates=True)
        # with returning='representation'. If a conflict is hit, supabase-py
        # returns an empty data list.
        resp = (
            get_supabase()
            .table(_TABLE)
            .upsert(row, on_conflict="bison_reply_uuid", ignore_duplicates=True)
            .execute()
        )
    except Exception as exc:
        logger.error("reply_drafts.claim_failed uuid=%s err=%s", payload.reply.uuid, exc)
        raise
    data = resp.data or []
    if not data:
        return None
    return data[0]


def get_by_uuid(bison_reply_uuid: str) -> Optional[Dict[str, Any]]:
    try:
        resp = (
            get_supabase()
            .table(_TABLE)
            .select("*")
            .eq("bison_reply_uuid", bison_reply_uuid)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        logger.error("reply_drafts.get_by_uuid_failed: %s", exc)
        return None
    rows = resp.data or []
    return rows[0] if rows else None


def finalize(
    draft_id: str,
    *,
    drafted_subject: str,
    drafted_body: str,
    confidence_composite: float,
    confidence_components: Dict[str, Any],
    human_review_needed: bool,
    review_reason: str,
    rule_gates_failed: Sequence[str],
    rag_examples_used: Sequence[int],
    model_primary: str,
    model_ensemble: str,
) -> None:
    """Update the row claimed by claim() with the finished draft."""
    update = {
        "drafted_subject": drafted_subject,
        "drafted_body": drafted_body,
        "confidence_composite": confidence_composite,
        "confidence_components": confidence_components,
        "human_review_needed": human_review_needed,
        "review_reason": review_reason,
        "rule_gates_failed": list(rule_gates_failed),
        "rag_examples_used": list(rag_examples_used),
        "model_primary": model_primary,
        "model_ensemble": model_ensemble,
    }
    try:
        get_supabase().table(_TABLE).update(update).eq("id", draft_id).execute()
    except Exception as exc:
        logger.error("reply_drafts.finalize_failed id=%s err=%s", draft_id, exc)
        raise


def delete(draft_id: str) -> None:
    """Used to roll back a claimed row when the drafter pipeline fails fatally."""
    try:
        get_supabase().table(_TABLE).delete().eq("id", draft_id).execute()
    except Exception as exc:
        logger.error("reply_drafts.delete_failed id=%s err=%s", draft_id, exc)


def list_pending(limit: int = 50) -> List[Dict[str, Any]]:
    """For the /admin/drafts endpoint."""
    try:
        resp = (
            get_supabase()
            .table(_TABLE)
            .select("id,bison_reply_uuid,lead_email,drafted_subject,confidence_composite,human_review_needed,created_at")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return resp.data or []
    except Exception as exc:
        logger.error("reply_drafts.list_pending_failed: %s", exc)
        return []
