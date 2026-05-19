"""
Composite confidence scoring for drafted replies.

  composite = 0.30 * llm_self_rating
            + 0.30 * ensemble_agreement   (cosine of body embeddings)
            + 0.25 * rag_retrieval_quality (mean similarity of top-K examples)
            + 0.15                          (rule_gate_pass == True)

When any rule gate fails the composite is forced to 0.0 and
`human_review_needed` is forced true with a populated `review_reason`.
"""
from __future__ import annotations

import re
from typing import List, Sequence, Tuple

from models.drafts import ClaudeDraft, ConfidenceComponents, VoiceExample

# --- Rule gates ---------------------------------------------------------------

# Saman's cold-email skill instructs Claude to wrap paragraphs in <p>...</p>,
# so we strip HTML before running structural regexes. We keep the raw body for
# anything content-sensitive (banned keywords, em-dash detection).
_HTML_TAG_RE = re.compile(r"<[^>]+>")
# Require a capitalized name token after the greeting so "Hi there," or
# "Hey everyone," don't pass — the skill mandates address-by-name.
_GREETING_RE = re.compile(r"^\s*(Hi|Hey|Hello)\s+[A-Z][A-Za-z]+", re.MULTILINE)
# Closer = any sign-off rotator from the skill ("Cheers", "Thanks", "Best",
# "Talk soon") or the literal name "Saman" appearing near the end (last ~12
# lines covers signature + title rotators without false-matching mid-body
# self-references).
_CLOSER_TOKEN_RE = re.compile(r"\b(Cheers|Thanks|Best|Talk soon|Saman)\b", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_PRICING_RE = re.compile(r"\$\s*\d")
_BARE_EMDASH_RE = re.compile(r"(?<! )—(?! )")


def _strip_html(text: str) -> str:
    """Strip HTML tags so rule regexes work on plain text.

    `<p>` and `<br>` become paragraph/line breaks; everything else is removed.
    The original `body` is what gets stored and rendered, this is only for
    structural rule-gate analysis.
    """
    if not text:
        return ""
    text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<p\b[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
    text = _HTML_TAG_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _has_closer(plain: str) -> bool:
    """True if any sign-off marker appears in the last few non-empty lines.

    URLs are stripped first because calendar links like
    `cal.com/saman/intro` would otherwise false-match the "Saman" alternative.
    """
    tail_lines = [l for l in plain.splitlines() if l.strip()][-12:]
    tail = _URL_RE.sub(" ", "\n".join(tail_lines))
    return bool(_CLOSER_TOKEN_RE.search(tail))

_BANNED_KEYWORDS = (
    "contract",
    "msa",
    "quote",
    "proposal pricing",
    "sign here",
    "lawyer",
    "audit fee",
)
_BANNED_PHRASES = (
    "i hope this finds you well",
    "looking forward to hearing from you",
    "don't hesitate to reach out",
    "do not hesitate to reach out",
)

_MIN_WORDS = 30
_MAX_WORDS = 700


def _word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


def evaluate_rule_gates(body: str) -> List[str]:
    """Return the list of failed gate names. Empty list = all passed."""
    failed: List[str] = []
    if not body or not body.strip():
        return ["body_empty"]

    plain = _strip_html(body)

    wc = _word_count(plain)
    if wc < _MIN_WORDS:
        failed.append(f"body_too_short ({wc} words)")
    if wc > _MAX_WORDS:
        failed.append(f"body_too_long ({wc} words)")

    if _PRICING_RE.search(plain):
        failed.append("pricing_leak")

    body_lower = plain.lower()
    for kw in _BANNED_KEYWORDS:
        if kw in body_lower:
            failed.append(f"banned_keyword:{kw}")
    for phrase in _BANNED_PHRASES:
        if phrase in body_lower:
            failed.append(f"banned_phrase:{phrase[:40]}")

    if not _GREETING_RE.search(plain):
        failed.append("missing_greeting")
    if not _has_closer(plain):
        failed.append("missing_closer")
    # The em-dash check runs against the original body — HTML stripping
    # doesn't affect punctuation.
    if _BARE_EMDASH_RE.search(body):
        failed.append("emdash_without_spaces")

    return failed


# --- Composite scoring --------------------------------------------------------

def rag_retrieval_quality(examples: Sequence[VoiceExample]) -> float:
    if not examples:
        return 0.0
    clamped = [max(0.0, min(1.0, e.similarity)) for e in examples]
    return sum(clamped) / len(clamped)


def composite_score(
    *,
    draft: ClaudeDraft,
    failed_gates: Sequence[str],
    ensemble_agreement: float,
    rag_quality: float,
) -> Tuple[float, ConfidenceComponents]:
    """Return (composite_float, ConfidenceComponents)."""
    llm_self = max(0.0, min(1.0, float(draft.confidence)))
    ensemble = max(0.0, min(1.0, float(ensemble_agreement)))
    rag_q = max(0.0, min(1.0, float(rag_quality)))
    rule_pass = len(failed_gates) == 0

    if rule_pass:
        composite = 0.30 * llm_self + 0.30 * ensemble + 0.25 * rag_q + 0.15
    else:
        composite = 0.0

    composite = round(max(0.0, min(1.0, composite)), 3)

    components = ConfidenceComponents(
        llm_self_rating=round(llm_self, 3),
        rule_gate_pass=rule_pass,
        rule_gates_failed=list(failed_gates),
        ensemble_agreement=round(ensemble, 3),
        rag_retrieval_quality=round(rag_q, 3),
        composite=composite,
    )
    return composite, components
