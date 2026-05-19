"""
Build a Slack-ready payload (Block Kit `blocks` + mrkdwn `text` fallback)
that n8n can pass straight to chat.postMessage.

Conventions enforced here so n8n stays dumb:
  - Slack mrkdwn uses *bold* (single asterisk), <url|label> for links, and
    requires escaping `&`, `<`, `>` in text fields.
  - Section block text is capped at 3000 chars by Slack — we truncate.
  - Header text is plain_text, capped at 150 chars — we truncate.
  - Block Kit messages cap at 50 blocks total — we stay well under.
  - Triple-backtick code fences preserve whitespace and stop literal `*` or
    `_` in the email body from being interpreted as Slack formatting.

References:
  https://docs.slack.dev/reference/methods/chat.postMessage
  https://docs.slack.dev/block-kit/
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from models.bison_payload import BisonLeadInterestedData
from models.drafts import ConfidenceComponents

_SECTION_TEXT_LIMIT = 2900   # leave headroom under Slack's 3000
_HEADER_TEXT_LIMIT = 145     # leave headroom under Slack's 150
_CODE_FENCE = "```"


def escape_mrkdwn(text: str) -> str:
    """Escape characters Slack interprets as entities inside mrkdwn text.

    Per Slack docs, when literal `&`, `<`, `>` appear inside a mrkdwn text
    field they must be HTML-escaped. Inside code fences this is unnecessary
    but harmless.
    """
    if not text:
        return ""
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "\u2026"  # single-char ellipsis


def _confidence_emoji(score: float) -> str:
    if score >= 0.80:
        return ":large_green_circle:"
    if score >= 0.55:
        return ":large_yellow_circle:"
    return ":red_circle:"


def _fenced(text: str, limit: int) -> str:
    """Wrap text in triple-backtick fences. Truncates inner text to fit limit.

    The limit applies to the *full* string including the two fence markers,
    so callers can compose this with other mrkdwn text within the same
    section block.
    """
    fence_overhead = len(_CODE_FENCE) * 2 + 2  # fences + leading/trailing newline
    inner = _truncate(text or "", max(0, limit - fence_overhead))
    return f"{_CODE_FENCE}\n{inner}\n{_CODE_FENCE}"


def _full_name(payload: BisonLeadInterestedData) -> str:
    lead = payload.lead
    parts = [p for p in (lead.first_name, lead.last_name) if p]
    return " ".join(parts) or lead.email or "(unknown)"


def build_plain_text(
    *,
    payload: BisonLeadInterestedData,
    subject: str,
    body: str,
    composite: float,
) -> str:
    """Plain mrkdwn fallback (the `text` field on chat.postMessage).

    Slack uses this in notifications and in clients that can't render Block
    Kit. Keep it short and informative.
    """
    name = _full_name(payload)
    company = payload.lead.company or ""
    parts = [
        f"*New positive reply* {_confidence_emoji(composite)} ({composite:.2f})",
        f"From: {escape_mrkdwn(name)}" + (f" at {escape_mrkdwn(company)}" if company else ""),
        f"Subject: {escape_mrkdwn(subject)}",
        "",
        body.strip(),
    ]
    return _truncate("\n".join(parts), _SECTION_TEXT_LIMIT)


def build_draft_blocks(
    *,
    draft_id: str,
    payload: BisonLeadInterestedData,
    clean_prospect_reply: str,
    subject: str,
    body: str,
    confidence: ConfidenceComponents,
    review_reason: str = "",
    rag_examples_used: Optional[List[int]] = None,
    model_primary: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Produce a Block Kit `blocks` array suitable for chat.postMessage.

    The layout:
      1. Header — "New positive reply" + emoji
      2. Section (fields) — From / Company / Email / Campaign / Confidence / Step
      3. Section — the prospect's reply (in a code fence)
      4. Divider
      5. Section — the drafted subject + body (in a code fence so literal
         email punctuation isn't reinterpreted as Slack formatting)
      6. Optional section — review reason (only if review needed)
      7. Context — draft_id, rag count, model
    """
    lead = payload.lead
    campaign = payload.campaign
    step = payload.scheduled_email.sequence_step_order if payload.scheduled_email else None
    composite_emoji = _confidence_emoji(confidence.composite)
    rag_count = len(rag_examples_used or [])

    blocks: List[Dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": _truncate(f":mailbox_with_mail: New positive reply", _HEADER_TEXT_LIMIT),
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*From:*\n{escape_mrkdwn(_full_name(payload))}"},
                {"type": "mrkdwn", "text": f"*Company:*\n{escape_mrkdwn(lead.company or '—')}"},
                {"type": "mrkdwn", "text": f"*Email:*\n{escape_mrkdwn(lead.email)}"},
                {"type": "mrkdwn", "text": f"*Campaign:*\n{escape_mrkdwn(campaign.name or str(campaign.id))}"},
                {
                    "type": "mrkdwn",
                    "text": f"*Confidence:*\n{confidence.composite:.2f} {composite_emoji}",
                },
                {"type": "mrkdwn", "text": f"*Step:*\n#{step}" if step is not None else "*Step:*\n—"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": _truncate(
                    "*Their reply:*\n" + _fenced(clean_prospect_reply, _SECTION_TEXT_LIMIT - 20),
                    _SECTION_TEXT_LIMIT,
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": _truncate(
                    f"*Drafted response*\n*Subject:* {escape_mrkdwn(subject)}\n"
                    + _fenced(body, _SECTION_TEXT_LIMIT - 60),
                    _SECTION_TEXT_LIMIT,
                ),
            },
        },
    ]

    if review_reason:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": _truncate(
                        f":warning: *Human review suggested:* {escape_mrkdwn(review_reason)}",
                        _SECTION_TEXT_LIMIT,
                    ),
                },
            }
        )

    context_pieces = [f"`draft_id:` `{draft_id}`", f"RAG: {rag_count} example(s)"]
    if model_primary:
        context_pieces.append(f"model: `{escape_mrkdwn(model_primary)}`")
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": " · ".join(context_pieces)}],
        }
    )

    return blocks


def build_slack_payload(
    *,
    draft_id: str,
    payload: BisonLeadInterestedData,
    clean_prospect_reply: str,
    subject: str,
    body: str,
    confidence: ConfidenceComponents,
    review_reason: str = "",
    rag_examples_used: Optional[List[int]] = None,
    model_primary: Optional[str] = None,
) -> Dict[str, Any]:
    """Return `{text, blocks}` ready to splat into chat.postMessage."""
    return {
        "text": build_plain_text(
            payload=payload,
            subject=subject,
            body=body,
            composite=confidence.composite,
        ),
        "blocks": build_draft_blocks(
            draft_id=draft_id,
            payload=payload,
            clean_prospect_reply=clean_prospect_reply,
            subject=subject,
            body=body,
            confidence=confidence,
            review_reason=review_reason,
            rag_examples_used=rag_examples_used,
            model_primary=model_primary,
        ),
    }
