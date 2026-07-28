-- ============================================================
-- Ottit CRM — Migration 013: InboxAssure placement results
-- Latest completed placement test results per inbox/domain,
-- written by workers/inboxassure_poller.py (read-only fetch —
-- the poller never launches tests on the InboxAssure side).
-- Run in Supabase SQL editor (v1 schema) or via psql.
-- ============================================================

create table if not exists inboxassure_placement_results (
    id bigint generated always as identity primary key,
    -- InboxAssure's identifier for the test (uuid/id from their API).
    ia_test_id text not null,
    domain text,
    inbox_email text,
    status text,
    -- Overall placement score (0-100) plus per-provider breakdown.
    overall_score numeric,
    google_score numeric,
    outlook_score numeric,
    -- Placement counts, when the API provides them.
    inbox_count integer,
    spam_count integer,
    missing_count integer,
    -- When the test completed on the InboxAssure side (shown prominently).
    test_completed_at timestamptz,
    test_created_at timestamptz,
    -- Full API payload for forward compatibility while docs are pending.
    raw jsonb,
    fetched_at timestamptz not null default now(),
    constraint uq_inboxassure_test unique (ia_test_id)
);

create index if not exists idx_ia_results_domain
    on inboxassure_placement_results (domain);

create index if not exists idx_ia_results_inbox
    on inboxassure_placement_results (inbox_email);

create index if not exists idx_ia_results_completed
    on inboxassure_placement_results (test_completed_at desc);
