"""
Thin wrapper around OpenAI's `text-embedding-3-small` (1536-dim).

Used by the drafter (to embed the prospect reply for RAG lookup), by
confidence scoring (to compare ensemble outputs), and by the voice-example
backfill admin endpoint.
"""
from __future__ import annotations

import logging
import math
import threading
from typing import List, Sequence

from tenacity import retry, stop_after_attempt, wait_exponential

from lib import config

logger = logging.getLogger(__name__)


_client = None
_client_lock = threading.Lock()


def _get_client():
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                from openai import OpenAI
                _client = OpenAI(api_key=config.OPENAI_API_KEY)
    return _client


@retry(reraise=True, stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10))
def embed(text: str) -> List[float]:
    """Embed a single string. Returns a 1536-d float list."""
    text = (text or "").strip()
    if not text:
        # OpenAI rejects empty input; return zero vector so callers don't crash.
        return [0.0] * 1536
    resp = _get_client().embeddings.create(
        model=config.OPENAI_EMBEDDING_MODEL,
        input=text,
    )
    return list(resp.data[0].embedding)


@retry(reraise=True, stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10))
def embed_many(texts: Sequence[str]) -> List[List[float]]:
    """Embed a batch in one API call. Empty strings get zero vectors."""
    cleaned = [(t or "").strip() for t in texts]
    non_empty_indices = [i for i, t in enumerate(cleaned) if t]
    if not non_empty_indices:
        return [[0.0] * 1536 for _ in cleaned]
    resp = _get_client().embeddings.create(
        model=config.OPENAI_EMBEDDING_MODEL,
        input=[cleaned[i] for i in non_empty_indices],
    )
    embeddings = [list(d.embedding) for d in resp.data]
    out: List[List[float]] = [[0.0] * 1536 for _ in cleaned]
    for idx, vec in zip(non_empty_indices, embeddings):
        out[idx] = vec
    return out


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity bounded to [-1, 1]. Returns 0 if either vector is null."""
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / (na * nb)))
