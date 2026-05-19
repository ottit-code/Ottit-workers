"""
RAG over Saman's historical replies stored in the Supabase `documents` table.

The `match_voice_examples` Postgres function (migration 008) does cosine
distance ranking server-side; we just feed it the query embedding and
unwrap the results into VoiceExample objects.
"""
from __future__ import annotations

import logging
from typing import List, Sequence

from lib import config
from lib.supabase_client import get_supabase
from models.drafts import VoiceExample

logger = logging.getLogger(__name__)


def retrieve_voice_examples(query_embedding: Sequence[float], k: int = config.RAG_TOP_K) -> List[VoiceExample]:
    """Return the top-K nearest voice examples for a given query embedding."""
    if not query_embedding:
        return []
    try:
        resp = get_supabase().rpc(
            "match_voice_examples",
            {"query_embedding": list(query_embedding), "match_count": k},
        ).execute()
    except Exception as exc:
        logger.warning("rag.match_voice_examples_failed: %s", exc)
        return []

    rows = resp.data or []
    out: List[VoiceExample] = []
    for row in rows:
        try:
            out.append(
                VoiceExample(
                    id=int(row["id"]),
                    content=row["content"],
                    similarity=float(row.get("similarity") or 0.0),
                    metadata=row.get("metadata") or {},
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug("rag.row_skipped: %s row=%s", exc, row)
    return out


def average_similarity(examples: Sequence[VoiceExample]) -> float:
    """Mean cosine similarity across retrieved examples — the RAG quality signal."""
    if not examples:
        return 0.0
    total = sum(max(0.0, min(1.0, e.similarity)) for e in examples)
    return total / len(examples)


def insert_voice_example(content: str, embedding: Sequence[float], metadata: dict) -> int | None:
    """Insert a new (prospect, saman) pair into `documents` for future RAG.

    `metadata.type` is forced to `'voice_example'` to match the existing
    Supabase convention used by 268k+ rows and the `match_voice_examples`
    SQL function. Callers should also pass a `source` tag (e.g.
    'historical_bison_backfill', 'manual_edit').
    """
    # `type` is the convention in the live documents table. Forced here so
    # callers can't drift; we still accept extra metadata keys.
    meta = {**metadata, "type": "voice_example"}
    try:
        resp = get_supabase().table("documents").insert({
            "content": content,
            "metadata": meta,
            "embedding": list(embedding),
        }).execute()
    except Exception as exc:
        logger.error("rag.insert_voice_example_failed: %s", exc)
        return None
    rows = resp.data or []
    if rows and isinstance(rows[0], dict):
        return rows[0].get("id")
    return None
