"""
Anthropic Claude client used by the drafter.

Two call modes:
  - call_primary  → CLAUDE_MODEL_PRIMARY at temp=0.7, the canonical draft.
  - call_ensemble → CLAUDE_MODEL_ENSEMBLE at temp=0.4, used to measure
                    ensemble agreement (cosine of body embeddings).

Both calls expect strict JSON back. If parsing fails we retry once with a
stricter instruction; if it still fails we raise so the caller can record
the error and surface 500 to n8n.

Newer Anthropic models (Claude 4.x and beyond) have deprecated the
`temperature` parameter. We detect this at runtime via the API's 400
response and remember per-model whether to send temperature on future
calls. This keeps older models working while gracefully adapting to new
ones without code changes.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any, Dict, Optional, Set

from tenacity import retry, stop_after_attempt, wait_exponential

from lib import config
from models.drafts import ClaudeDraft

logger = logging.getLogger(__name__)

_client = None
_client_lock = threading.Lock()

_MAX_TOKENS = 1024
_PRIMARY_TEMP = 0.7
_ENSEMBLE_TEMP = 0.4
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
_STRICT_RETRY_NUDGE = (
    "\n\nIMPORTANT: Your previous response was not valid JSON. Return ONLY a "
    "single JSON object exactly matching the schema. No prose, no markdown, "
    "no code fences."
)

# Per-process cache: models that have rejected `temperature` once will be
# called without it for the rest of this process's lifetime.
_NO_TEMPERATURE_MODELS: Set[str] = set()
_NO_TEMP_LOCK = threading.Lock()


def _get_client():
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                from anthropic import Anthropic
                _client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


class ClaudeError(RuntimeError):
    """Raised when Claude cannot produce a valid draft after a retry."""


def _parse_json_block(text: str) -> Optional[Dict[str, Any]]:
    """Tolerantly extract a JSON object from a Claude response."""
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = _JSON_BLOCK.search(text)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _is_temperature_deprecated_error(exc: Exception) -> bool:
    """True if Anthropic rejected the call specifically because `temperature` is deprecated."""
    msg = str(exc).lower()
    return "temperature" in msg and "deprecated" in msg


def _create_message(client, model: str, system: str, user: str, temperature: float):
    """Single API call. Strips `temperature` for models that have rejected it before."""
    with _NO_TEMP_LOCK:
        skip_temp = model in _NO_TEMPERATURE_MODELS

    kwargs: Dict[str, Any] = {
        "model": model,
        "max_tokens": _MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    if not skip_temp:
        kwargs["temperature"] = temperature

    try:
        return client.messages.create(**kwargs)
    except Exception as exc:
        if not skip_temp and _is_temperature_deprecated_error(exc):
            logger.info(
                "anthropic.temperature_deprecated_for_model model=%s retrying_without_temperature",
                model,
            )
            with _NO_TEMP_LOCK:
                _NO_TEMPERATURE_MODELS.add(model)
            kwargs.pop("temperature", None)
            return client.messages.create(**kwargs)
        raise


@retry(reraise=True, stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10))
def _invoke(model: str, system: str, user: str, temperature: float) -> str:
    client = _get_client()
    response = _create_message(client, model, system, user, temperature)
    chunks = []
    for block in response.content:
        text = getattr(block, "text", None)
        if text:
            chunks.append(text)
    return "".join(chunks)


def _call(model: str, system: str, user: str, temperature: float) -> ClaudeDraft:
    raw = _invoke(model, system, user, temperature)
    parsed = _parse_json_block(raw)
    if parsed is None:
        logger.warning("anthropic.json_parse_failed model=%s retrying", model)
        raw = _invoke(model, system + _STRICT_RETRY_NUDGE, user, temperature)
        parsed = _parse_json_block(raw)
    if parsed is None:
        raise ClaudeError(f"Claude ({model}) did not return valid JSON after retry")
    try:
        return ClaudeDraft.model_validate(parsed)
    except Exception as exc:
        raise ClaudeError(f"Claude ({model}) JSON failed schema: {exc}") from exc


def call_primary(system: str, user: str) -> ClaudeDraft:
    """Generate the canonical draft using the configured primary model."""
    return _call(config.CLAUDE_MODEL_PRIMARY, system, user, _PRIMARY_TEMP)


def call_ensemble(system: str, user: str) -> ClaudeDraft:
    """Generate a second draft with a different model + lower temperature.

    Used only to measure ensemble agreement; never returned to n8n.
    """
    return _call(config.CLAUDE_MODEL_ENSEMBLE, system, user, _ENSEMBLE_TEMP)
