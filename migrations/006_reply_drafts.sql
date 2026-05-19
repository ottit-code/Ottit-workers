-- ============================================================
-- Ottit Drafter — Migration 006: reply_drafts table
-- Stores AI-generated reply drafts keyed by Bison reply UUID
-- for race-safe idempotency. One row per LEAD_INTERESTED event.
-- ============================================================

create table if not exists public.reply_drafts (
    id                     uuid primary key default gen_random_uuid(),
    reply_id               text not null,
    bison_reply_uuid       text not null unique,
    drafted_subject        text not null,
    drafted_body           text not null,
    confidence_composite   numeric(4,3) not null default 0,
    confidence_components  jsonb not null default '{}'::jsonb,
    human_review_needed    boolean not null default false,
    review_reason          text,
    rule_gates_failed      text[] not null default '{}',
    sender_email           text not null,
    sender_email_id        bigint,
    lead_email             text not null,
    lead_id                bigint,
    bison_reply_id         bigint,
    reply_message_id       text,
    original_message_id    text,
    rag_examples_used      bigint[] not null default '{}',
    model_primary          text,
    model_ensemble         text,
    created_at             timestamptz not null default now()
);

create index if not exists idx_reply_drafts_reply_id   on public.reply_drafts(reply_id);
create index if not exists idx_reply_drafts_lead_id    on public.reply_drafts(lead_id);
create index if not exists idx_reply_drafts_created_at on public.reply_drafts(created_at desc);

comment on table public.reply_drafts is
  'AI-generated reply drafts. One row per LEAD_INTERESTED event; bison_reply_uuid enforces idempotency.';
