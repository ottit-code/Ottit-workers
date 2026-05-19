"""
Tests for lib/rag.py that lock in the metadata-key contract.

The live Supabase project uses `metadata->>'type' = 'voice_example'` as the
discriminator (268k+ rows) and the partial ivfflat index
`idx_documents_voice_example_embedding` filters on that exact predicate.
If the worker ever wrote `doc_type` instead, rows would land outside the
partial index and the RAG retrieval would silently degrade. These tests
fail loudly if that contract ever drifts.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from lib import rag


def _capture_insert():
    """Return (supabase_mock, captured_row_dict) — captured updates in-place."""
    captured: dict = {}
    sb = MagicMock()

    def insert(row):
        captured.update(row)
        chain = MagicMock()
        chain.execute.return_value.data = [{"id": 42}]
        return chain

    sb.table.return_value.insert.side_effect = insert
    return sb, captured


def test_insert_voice_example_forces_metadata_type_key():
    sb, captured = _capture_insert()
    with patch("lib.rag.get_supabase", return_value=sb):
        new_id = rag.insert_voice_example(
            content="PROSPECT: ...\n\nSAMAN: ...",
            embedding=[0.0] * 1536,
            metadata={"source": "historical_bison_backfill", "weight": 1.0},
        )
    assert new_id == 42
    assert captured["metadata"]["type"] == "voice_example"
    # Must NOT use the wrong key name.
    assert "doc_type" not in captured["metadata"]
    # Other metadata is preserved.
    assert captured["metadata"]["source"] == "historical_bison_backfill"


def test_insert_voice_example_overrides_wrong_key():
    """Even if a caller accidentally passes `doc_type`, the row still ends
    up with the canonical `type` key (and the wrong key is harmless but
    present — we don't strip it, just guarantee the right one)."""
    sb, captured = _capture_insert()
    with patch("lib.rag.get_supabase", return_value=sb):
        rag.insert_voice_example(
            content="x",
            embedding=[0.0] * 1536,
            metadata={"doc_type": "voice_example", "source": "test"},
        )
    assert captured["metadata"]["type"] == "voice_example"


def test_insert_voice_example_cannot_be_overridden_to_wrong_value():
    """If a caller passes `type` with a non-voice_example value, the
    forced override wins so the row always lands in the partial index."""
    sb, captured = _capture_insert()
    with patch("lib.rag.get_supabase", return_value=sb):
        rag.insert_voice_example(
            content="x",
            embedding=[0.0] * 1536,
            metadata={"type": "reply", "source": "test"},
        )
    assert captured["metadata"]["type"] == "voice_example"
