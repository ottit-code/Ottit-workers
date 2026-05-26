"""
Admin endpoints for the drafter:

  POST /admin/backfill-voice-examples
      Walks `reply_events` (already populated by reply_events_poller) +
      Bison conversation threads, pairs each prospect reply with the
      Saman response that followed, embeds the prospect text, and inserts
      into `documents` with metadata.type='voice_example' (matches the
      existing Supabase convention used by ~268k rows and the
      `match_voice_examples` SQL function).

  GET /admin/drafts
      Lists the most recent reply_drafts rows for spot-checking.

Auth: Bearer ADMIN_API_KEY.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, Security, HTTPException
from pydantic import BaseModel, Field

from api.routers.drafter_deps import require_admin_key
from lib import drafter, emailbison, openai_embed, rag, reply_drafts_dao, reply_parser
from lib.supabase_client import get_supabase
from models.bison_payload import (
    BisonCampaign,
    BisonCustomVariable,
    BisonLead,
    BisonLeadInterestedData,
    BisonReply,
    BisonScheduledEmail,
    BisonSenderEmail,
)
from prompts.templates import format_voice_example_content

logger = logging.getLogger(__name__)

router = APIRouter(tags=["drafter-admin"])


class BackfillRequest(BaseModel):
    lookback_days: int = Field(default=180, ge=1, le=730)
    max_count: int = Field(default=200, ge=1, le=2000)
    sender_email_filter: Optional[str] = None
    classifications: List[str] = Field(
        default_factory=lambda: ["interested", "question", "not_interested"],
        description=(
            "Which reply_events.classification values to pull. Defaults to real "
            "human replies where Saman likely responded. Pass an empty list to "
            "disable the filter (NOT recommended — automated_reply / ooo will "
            "waste Bison + OpenAI quota and yield no pairs)."
        ),
    )
    dry_run: bool = False


class BackfillResponse(BaseModel):
    candidates_seen: int
    pairs_inserted: int
    skipped_empty_prospect: int
    skipped_no_saman_response: int
    skipped_dup_or_error: int
    inserted_ids: List[int]


@router.post(
    "/admin/backfill-voice-examples",
    response_model=BackfillResponse,
    dependencies=[Security(require_admin_key)],
)
def backfill_voice_examples(body: BackfillRequest) -> BackfillResponse:
    """One-shot script-as-endpoint. Safe to re-run; new examples just add to RAG."""
    from datetime import datetime, timedelta, timezone

    supabase = get_supabase()
    since = (datetime.now(timezone.utc).date() - timedelta(days=body.lookback_days)).isoformat()

    # Pull candidate reply_events: anything classified as interested (the
    # historical positive replies — the same population the drafter handles).
    query = (
        supabase.table("reply_events")
        .select("reply_id,campaign_id,campaign_name,lead_id,lead_email,sender_email,replied_at,subject,classification")
        .gte("replied_at", since)
        .order("replied_at", desc=True)
        .limit(body.max_count)
    )
    if body.sender_email_filter:
        query = query.eq("sender_email", body.sender_email_filter)
    if body.classifications:
        query = query.in_("classification", body.classifications)

    candidates = query.execute().data or []

    inserted: List[int] = []
    empty_prospect = 0
    no_saman = 0
    dup_or_error = 0

    for row in candidates:
        try:
            pair = _build_voice_pair(row)
        except Exception as exc:
            logger.warning("backfill.pair_failed reply_id=%s err=%s", row.get("reply_id"), exc)
            dup_or_error += 1
            continue

        if pair is None:
            no_saman += 1
            continue

        prospect_text, saman_text = pair
        if not prospect_text.strip():
            empty_prospect += 1
            continue

        content = format_voice_example_content(prospect_text, saman_text)

        if body.dry_run:
            inserted.append(-1)
            continue

        try:
            embedding = openai_embed.embed(prospect_text)
        except Exception as exc:
            logger.warning("backfill.embed_failed reply_id=%s err=%s", row.get("reply_id"), exc)
            dup_or_error += 1
            continue

        new_id = rag.insert_voice_example(
            content=content,
            embedding=embedding,
            metadata={
                "source": "historical_bison_backfill",
                "weight": 1.0,
                "campaign_id": row.get("campaign_id"),
                "campaign_name": row.get("campaign_name"),
                "date": row.get("replied_at"),
                "reply_id": row.get("reply_id"),
            },
        )
        if new_id is None:
            dup_or_error += 1
        else:
            inserted.append(new_id)

    return BackfillResponse(
        candidates_seen=len(candidates),
        pairs_inserted=len(inserted),
        skipped_empty_prospect=empty_prospect,
        skipped_no_saman_response=no_saman,
        skipped_dup_or_error=dup_or_error,
        inserted_ids=inserted,
    )


def _build_voice_pair(reply_event: Dict[str, Any]) -> Optional[tuple[str, str]]:
    """Given one `reply_events` row, walk the Bison conversation thread to
    pair the prospect's reply with the Saman-side response that followed.

    Identification strategy:
      - `prospect_email` is taken from `reply_events.lead_email` (the prospect side).
      - Anything NOT from prospect_email is treated as "Saman's side" so we
        capture replies from any of his sender aliases (saman@, azita@,
        amy@, etc.). reply_events.sender_email is unreliable for this
        because Saman often follows up from a different alias than the
        original outbound.
    """
    reply_id = reply_event.get("reply_id")
    if reply_id is None:
        return None

    try:
        thread_raw = emailbison.get(f"/api/replies/{reply_id}/conversation-thread")
    except Exception as exc:
        logger.debug("backfill.thread_fetch_failed reply_id=%s err=%s", reply_id, exc)
        return None

    messages = _flatten_thread(thread_raw)
    if not messages:
        return None

    prospect_email = (reply_event.get("lead_email") or "").lower()
    if not prospect_email:
        return None

    prospect_text = ""
    saman_text = ""
    for msg in messages:
        sender = (msg.get("from_email_address") or "").lower()
        body = msg.get("text_body") or ""
        if not body.strip() or not sender:
            continue
        body = reply_parser.strip_quoted_thread(body).strip()
        if not body:
            continue

        is_prospect = sender == prospect_email

        if not prospect_text and is_prospect:
            prospect_text = body
            continue

        if prospect_text and not is_prospect:
            saman_text = body
            break

    if not prospect_text or not saman_text:
        return None
    return prospect_text, saman_text


def _flatten_thread(raw: Any) -> List[Dict[str, Any]]:
    """Flatten Bison's `{current_reply, older_messages, newer_messages}` shape
    into a single chronologically-sorted list of message dicts.

    Also tolerates legacy/list-shaped responses for forward compatibility.
    """
    if isinstance(raw, list):
        return _sort_by_date([m for m in raw if isinstance(m, dict)])

    if not isinstance(raw, dict):
        return []

    # The canonical Bison shape wraps everything under `data`.
    inner = raw.get("data") if isinstance(raw.get("data"), (dict, list)) else raw

    if isinstance(inner, list):
        return _sort_by_date([m for m in inner if isinstance(m, dict)])

    if isinstance(inner, dict):
        msgs: List[Dict[str, Any]] = []
        for key in ("older_messages", "newer_messages", "messages", "conversation", "thread"):
            v = inner.get(key)
            if isinstance(v, list):
                msgs.extend(m for m in v if isinstance(m, dict))
        cur = inner.get("current_reply")
        if isinstance(cur, dict):
            msgs.append(cur)
        if msgs:
            return _sort_by_date(msgs)
        # Last resort: looks like a single message dict.
        if "text_body" in inner or "from_email_address" in inner:
            return [inner]

    return []


def _sort_by_date(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sort messages chronologically by date_received (asc).

    Messages missing the field sort last, preserving insertion order among them.
    """
    def key(m: Dict[str, Any]) -> str:
        return m.get("date_received") or m.get("created_at") or "9999"
    return sorted(msgs, key=key)


# --- /admin/drafts ---------------------------------------------------------

@router.get(
    "/admin/drafts",
    dependencies=[Security(require_admin_key)],
)
def list_drafts(limit: int = 50):
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be in [1, 500]")
    return reply_drafts_dao.list_pending(limit=limit)


# --- /admin/voice-example-stats -------------------------------------------
#
# The partial ivfflat index `idx_documents_voice_example_embedding` was
# built with `lists = 10`, sized for ≤ ~500 rows in the voice_example
# subset (sqrt-of-N heuristic). As the backfill grows the corpus this
# endpoint flags when the index needs re-tuning.

_LISTS_TUNING_THRESHOLDS = [
    # (corpus_size_lower_bound, recommended_lists, action)
    (500,    50,  "REINDEX with lists=50 (or drop + recreate)"),
    (5000,   100, "REINDEX with lists=100"),
    (10000,  250, "REINDEX with lists=250, consider switching to HNSW"),
    (50000,  500, "Switch to HNSW index — ivfflat starts to degrade past 50k"),
]


class VoiceExampleStats(BaseModel):
    voice_example_count: int
    current_lists: int = 10
    recommended_action: Optional[str] = None
    recommended_lists: Optional[int] = None
    healthy: bool


@router.get(
    "/admin/voice-example-stats",
    response_model=VoiceExampleStats,
    dependencies=[Security(require_admin_key)],
)
def voice_example_stats() -> VoiceExampleStats:
    try:
        resp = (
            get_supabase()
            .table("documents")
            .select("id", count="exact")
            .eq("metadata->>type", "voice_example")
            .limit(1)
            .execute()
        )
        count = int(resp.count or 0)
    except Exception as exc:
        logger.error("voice_example_stats.count_failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Count failed: {exc}")

    rec_action: Optional[str] = None
    rec_lists: Optional[int] = None
    for threshold, lists, action in _LISTS_TUNING_THRESHOLDS:
        if count >= threshold:
            rec_action = action
            rec_lists = lists

    return VoiceExampleStats(
        voice_example_count=count,
        current_lists=10,
        recommended_action=rec_action,
        recommended_lists=rec_lists,
        healthy=rec_action is None,
    )


# --- /admin/draft-test ----------------------------------------------------
#
# Convenience endpoint for ad-hoc testing of the drafter pipeline without
# crafting a full Bison webhook payload. Pass just the prospect's email body
# (plus optional context) and we build a synthetic BisonLeadInterestedData
# under the hood and run it through `drafter.run` exactly like the real
# webhook would.
#
# WARNING: this hits paid LLM APIs (Claude + OpenAI embeddings) and writes a
# real row to v1.reply_drafts. Use sparingly. Test rows are easy to spot —
# they default to lead_email='test-lead@example.com' and the bison_reply_uuid
# is freshly minted per call so collisions never happen.


class DraftTestRequest(BaseModel):
    """Minimum: just `email_body`. Everything else has sensible defaults so a
    one-line curl works."""
    email_body: str = Field(
        ...,
        min_length=1,
        description="Raw text of the prospect's reply. Quoted-thread will be stripped.",
    )

    # Optional context — fills the same fields the real Bison payload supplies.
    subject: str = "Re: quick question"
    lead_first_name: str = "Friend"
    lead_last_name: Optional[str] = None
    lead_email: str = "test-lead@example.com"
    lead_company: Optional[str] = None
    lead_title: Optional[str] = None
    lead_id: int = 0
    sender_email: str = "saman@ottit.com"
    sender_email_id: int = 0
    campaign_id: int = 0
    campaign_name: str = "Manual draft test"
    custom_variables: Dict[str, str] = Field(default_factory=dict)


def _build_synthetic_payload(req: DraftTestRequest) -> BisonLeadInterestedData:
    """Map a DraftTestRequest into the real BisonLeadInterestedData shape."""
    return BisonLeadInterestedData(
        reply=BisonReply(
            id=0,
            uuid=str(uuid4()),  # fresh per call so we never trip idempotency
            text_body=req.email_body,
            email_subject=req.subject,
            from_email_address=req.lead_email,
            from_name=" ".join(filter(None, [req.lead_first_name, req.lead_last_name])) or None,
        ),
        lead=BisonLead(
            id=req.lead_id,
            first_name=req.lead_first_name,
            last_name=req.lead_last_name,
            email=req.lead_email,
            title=req.lead_title,
            company=req.lead_company,
            custom_variables=[
                BisonCustomVariable(name=k, value=v) for k, v in req.custom_variables.items()
            ],
        ),
        campaign=BisonCampaign(id=req.campaign_id, name=req.campaign_name),
        scheduled_email=BisonScheduledEmail(),
        sender_email=BisonSenderEmail(id=req.sender_email_id, email=req.sender_email),
    )


# Late import to avoid a circular dep at module load time. The inbound router
# already exposes these — we reuse them so the response shape is identical.
from api.routers.drafter_inbound import (  # noqa: E402
    ContextBlock,
    DraftBody,
    DraftResponse,
)


@router.post(
    "/admin/draft-test",
    response_model=DraftResponse,
    dependencies=[Security(require_admin_key)],
)
def draft_test(body: DraftTestRequest) -> DraftResponse:
    """Drive the drafter with a raw email body (no Bison webhook required).

    Returns the same JSON shape as POST /webhooks/bison/lead-interested so
    you can sanity-check Slack rendering, voice adherence, and confidence
    scoring without going through n8n.
    """
    payload = _build_synthetic_payload(body)
    try:
        result = drafter.run(payload)
    except drafter.DrafterError as exc:
        logger.error("draft_test.drafter_error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:
        logger.exception("draft_test.unexpected_error")
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
