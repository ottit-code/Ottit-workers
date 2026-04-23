-- ============================================================
-- Ottit CRM — Migration 004: reply_review_state table
-- Persists per-reply human review state keyed on EmailBison reply_id.
-- Used by /replies, /counts, /activity-feed, and PATCH /actions/replies/{id}/*.
-- Run in Supabase SQL editor or via psql.
-- ============================================================

create table if not exists reply_review_state (
    reply_id        text primary key,
    review_state    text not null default 'pending'
                    check (review_state in ('pending', 'classified', 'snoozed', 'archived')),
    read            boolean not null default false,
    first_read_at   timestamptz,
    classification  text
                    check (classification in ('interested', 'not_interested', 'question', 'auto_reply', 'ooo')),
    updated_at      timestamptz not null default now()
);

create index if not exists idx_reply_review_state_pending
    on reply_review_state(review_state)
    where review_state = 'pending';

create index if not exists idx_reply_review_state_unread
    on reply_review_state(read)
    where read = false;
