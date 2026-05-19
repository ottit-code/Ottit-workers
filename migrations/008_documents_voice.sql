-- ============================================================
-- Ottit Drafter — Migration 008: documents + match_voice_examples
--
-- IMPORTANT: as of 2026-05-20 this migration is mostly a no-op against the
-- live Ottit project (bjwdvirjavawleeajhqv) — the `documents` table, the
-- `vector` extension, and the `match_voice_examples` function ALL already
-- exist and follow the convention `metadata->>'type' = 'voice_example'`
-- (used by ~268k existing rows: reply, lead, scheduled_email, etc.).
--
-- The only piece that was missing in production was a partial ivfflat
-- index aligned to the function's WHERE clause. We create just that here.
--
-- Everything else is `CREATE ... IF NOT EXISTS` and `CREATE OR REPLACE`
-- so re-running on a fresh project still bootstraps correctly.
-- ============================================================

create extension if not exists vector;

-- Bootstrap table (no-op on the live project; full create on fresh ones).
create table if not exists public.documents (
    id         bigserial primary key,
    content    text not null,
    metadata   jsonb not null default '{}'::jsonb,
    embedding  vector(1536),
    created_at timestamptz not null default now()
);

create index if not exists idx_documents_type
    on public.documents ((metadata->>'type'));

-- Partial ivfflat over just the voice_example rows.
-- This is what production needed: a full-table ivfflat over 268k vectors
-- exhausts the default maintenance_work_mem (64 MB), and even if built, the
-- planner rarely uses a global vector index when a metadata WHERE clause is
-- in play. Filtering the index on the same predicate the function uses lets
-- Postgres always reach for it.
--
-- TUNING ROADMAP (rule of thumb: `lists ≈ sqrt(rows_in_subset)`):
--   * 0 – 500 voice_examples   → lists = 10        (current)
--   * 500 – 5,000              → lists = 50–100,   REINDEX after growth
--   * 5,000 – 50,000           → lists = 100–250
--   * > 50,000                 → consider switching to HNSW
-- Re-tune by dropping + recreating this index (or REINDEX CONCURRENTLY).
-- The /admin/voice-example-stats endpoint will flag when re-tuning is due.
create index if not exists idx_documents_voice_example_embedding
    on public.documents using ivfflat (embedding vector_cosine_ops)
    with (lists = 10)
    where metadata->>'type' = 'voice_example';

comment on table public.documents is
  'RAG corpus. Voice examples used by the drafter live where metadata->>''type'' = ''voice_example''.';

-- RPC used by lib/rag.py — convention is metadata->>'type' (NOT 'doc_type').
-- We CREATE OR REPLACE so fresh projects get the right body; on the live
-- project this is a no-op since the function already exists with this body.
create or replace function public.match_voice_examples(
    query_embedding vector(1536),
    match_count int default 5
)
returns table (
    id         bigint,
    content    text,
    metadata   jsonb,
    similarity float
)
language sql stable as $$
    select
        d.id,
        d.content,
        d.metadata,
        1 - (d.embedding <=> query_embedding) as similarity
    from public.documents d
    where d.metadata->>'type' = 'voice_example'
      and d.embedding is not null
    order by d.embedding <=> query_embedding
    limit match_count;
$$;

comment on function public.match_voice_examples is
  'Top-K voice examples by cosine similarity. Filters metadata->>''type''=''voice_example''.';
