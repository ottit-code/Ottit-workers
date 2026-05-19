"""
Strip quoted-thread content from a prospect's reply so the model sees only
the new text they wrote, not the cold-email body re-quoted underneath.

We wrap `email_reply_parser` and fall back to manual cleanup if the package
is unavailable or produces something empty.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

try:
    from email_reply_parser import EmailReplyParser  # type: ignore
    _HAS_LIB = True
except Exception:  # pragma: no cover - import-time guard
    EmailReplyParser = None  # type: ignore
    _HAS_LIB = False


_QUOTE_LINE = re.compile(r"^\s*>.*$", re.MULTILINE)
_ON_WROTE = re.compile(
    r"^On\s.+?wrote:\s*$.*",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)
_FROM_HEADER_BLOCK = re.compile(
    r"^\s*(From|De|Von):\s.+$.*",
    re.MULTILINE | re.DOTALL,
)
_FORWARDED = re.compile(
    r"^-+\s*Forwarded message\s*-+.*",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)


def _fallback_strip(text: str) -> str:
    if not text:
        return ""
    out = text
    out = _ON_WROTE.sub("", out)
    out = _FORWARDED.sub("", out)
    out = _FROM_HEADER_BLOCK.sub("", out)
    out = _QUOTE_LINE.sub("", out)
    return out.strip()


def strip_quoted_thread(text: str) -> str:
    """Return only the prospect's freshly-written portion of their reply.

    Empty input returns empty string. Errors fall through to fallback regex
    so the drafter pipeline never crashes here.
    """
    if not text:
        return ""

    if _HAS_LIB:
        try:
            reply = EmailReplyParser.parse_reply(text)  # type: ignore[union-attr]
            stripped = (reply or "").strip()
            if stripped:
                return stripped
            logger.debug("reply_parser.lib_returned_empty falling_back_to_regex")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("reply_parser.lib_error: %s", exc)

    return _fallback_strip(text)
