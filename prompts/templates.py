"""
Prompt construction. Pure functions; no I/O.

Composes the five-layer prompt described in the plan:
  Layer 1 — instructions.md (system)
  Layer 2 — SKILL.md from saman-voice.skill (system)
  Layer 3 — RAG voice examples (user)
  Layer 4 — lead context (user)
  Layer 5 — the prospect's stripped reply (user)
"""
from __future__ import annotations

from typing import Iterable, Mapping, Optional

from models.bison_payload import BisonLeadInterestedData
from models.drafts import VoiceExample


OUTPUT_SCHEMA_INSTRUCTION = """\
Respond with ONLY a single JSON object — no prose, no code fences. Schema:
{
  "subject": string,        // include "Re: " prefix if missing
  "body":    string,        // plain text, signed off as Saman
  "confidence": number,     // 0-1, your own honest rating of the draft quality
  "human_review_needed": boolean,
  "review_reason": string   // empty if human_review_needed is false
}
"""


def build_system_prompt(instructions_md: str, skill_md: str) -> str:
    """Assemble the system prompt from Layer 1 + Layer 2 + output schema."""
    parts = []
    if instructions_md.strip():
        parts.append(instructions_md.strip())
    if skill_md.strip():
        parts.append("# Procedural Skill\n\n" + skill_md.strip())
    parts.append(OUTPUT_SCHEMA_INSTRUCTION)
    return "\n\n---\n\n".join(parts)


def _format_lead_context(
    payload: BisonLeadInterestedData,
    lead_snapshot: Optional[Mapping] = None,
) -> str:
    lead = payload.lead
    campaign = payload.campaign
    sched = payload.scheduled_email

    lines = ["LEAD CONTEXT:"]
    full_name = " ".join(p for p in [lead.first_name, lead.last_name] if p) or "(unknown)"
    lines.append(f"  name: {full_name}")
    if lead.email:
        lines.append(f"  email: {lead.email}")
    if lead.title:
        lines.append(f"  role: {lead.title}")
    if lead.company:
        lines.append(f"  company: {lead.company}")
    if campaign and (campaign.name or campaign.id):
        lines.append(f"  campaign: {campaign.name or campaign.id}")
    if sched and sched.sequence_step_order is not None:
        lines.append(f"  sequence_step: #{sched.sequence_step_order}")

    if lead.custom_variables:
        cv_lines = []
        for cv in lead.custom_variables:
            if cv.value:
                cv_lines.append(f"    - {cv.name}: {cv.value}")
        if cv_lines:
            lines.append("  custom_variables:")
            lines.extend(cv_lines)

    if lead_snapshot:
        snap_lines = _format_snapshot(lead_snapshot)
        if snap_lines:
            lines.append("  engagement_snapshot:")
            lines.extend(snap_lines)

    return "\n".join(lines)


def _format_snapshot(snap: Mapping) -> list[str]:
    """Compact prompt rendering of the lead_engagement_snapshots row.

    Selected columns: engagement_score, funnel_stage, campaign_engagements,
    tags, status. The two JSON-shaped columns are summarized rather than
    dumped verbatim — full payloads carry created_at/updated_at noise that
    burns tokens without helping Claude write a reply.
    """
    lines: list[str] = []

    if (score := snap.get("engagement_score")) is not None:
        lines.append(f"    - engagement_score: {score}")
    if stage := snap.get("funnel_stage"):
        lines.append(f"    - funnel_stage: {stage}")
    if status := snap.get("status"):
        lines.append(f"    - status: {status}")

    tags = snap.get("tags") or []
    if tag_names := [t["name"] for t in tags if isinstance(t, Mapping) and t.get("name")]:
        lines.append(f"    - tags: {', '.join(tag_names)}")

    engagements = snap.get("campaign_engagements") or []
    summarized = [
        _summarize_engagement(e) for e in engagements if isinstance(e, Mapping)
    ]
    if summarized:
        lines.append("    - campaigns:")
        lines.extend(f"      - {s}" for s in summarized)

    return lines


def _summarize_engagement(e: Mapping) -> str:
    """One-line summary of a single campaign_engagements entry."""
    cid = e.get("campaign_id")
    status = e.get("status") or "unknown"
    sent = e.get("emails_sent") or 0
    opens = e.get("opens") or 0
    replies = e.get("replies") or 0
    interested = " interested=true" if e.get("interested") else ""
    return f"campaign={cid} status={status} sent={sent} opens={opens} replies={replies}{interested}"


def _format_voice_examples(examples: Iterable[VoiceExample]) -> str:
    examples = list(examples)
    if not examples:
        return "FEW-SHOT EXAMPLES: (none retrieved — rely on the persona above)"
    blocks = ["FEW-SHOT EXAMPLES (most similar past Saman replies):"]
    for i, ex in enumerate(examples, 1):
        blocks.append(f"\nEXAMPLE {i} (similarity={ex.similarity:.2f})\n{ex.content.strip()}")
    return "\n".join(blocks)


def build_user_message(
    payload: BisonLeadInterestedData,
    clean_prospect_reply: str,
    voice_examples: Iterable[VoiceExample],
    lead_snapshot: Optional[Mapping] = None,
) -> str:
    """Assemble Layer 3 + Layer 4 + Layer 5 into the user message."""
    parts = [
        _format_lead_context(payload, lead_snapshot),
        _format_voice_examples(voice_examples),
        "CURRENT PROSPECT REPLY (this is the message Saman needs to respond to):",
        clean_prospect_reply.strip() or "(empty body)",
        "Draft Saman's response now as the JSON object specified.",
    ]
    return "\n\n".join(parts)


def format_voice_example_content(prospect_text: str, saman_text: str) -> str:
    """Canonical formatting for storing voice examples in `documents.content`."""
    return (
        "--- PROSPECT WROTE ---\n"
        f"{prospect_text.strip()}\n"
        "--- SAMAN RESPONDED ---\n"
        f"{saman_text.strip()}"
    )
