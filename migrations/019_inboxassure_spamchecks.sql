-- ============================================================
-- Ottit CRM — Migration 019: InboxAssure spamcheck.completed
--
-- Stores InboxAssure webhook payloads (event: spamcheck.completed)
-- forwarded from n8n. Parent run + overall_results, plus one child
-- row per account in `reports[]`.
--
-- Distinct from:
--   v1.inboxassure_placement_results  — API poller (placement tests)
--   v1.warmup_daily_report            — Bison warmup fleet buckets
--   spam_filter_tests                 — EmailGuard spam-filter tests
--
-- Schema: v1 (matches ClientOptions(schema="v1") in lib/supabase_client.py).
-- Idempotent. No RLS (workers use service role; same as 015/017).
-- ============================================================

create schema if not exists v1;

-- Parent: one row per InboxAssure spamcheck run
create table if not exists v1.inboxassure_spamchecks (
    -- InboxAssure spamcheck.id (numeric in webhook; stored as bigint)
    ia_spamcheck_id           bigint      primary key,
    name                      text,
    status                    text,
    is_domain_based           boolean,
    subject                   text,
    -- Email body used for the check (payload.spamcheck.body)
    email_body                text,
    conditions                text,
    ia_created_at             timestamptz,
    ia_updated_at             timestamptz,

    -- overall_results (flattened for querying / dashboards)
    total_accounts            integer,
    good_accounts             integer,
    bad_accounts              integer,
    good_accounts_percentage  numeric,
    bad_accounts_percentage   numeric,
    average_google_score      numeric,
    average_outlook_score     numeric,
    total_bounced             integer,
    total_unique_replies      integer,
    total_emails_sent         integer,

    -- Ottit workspace key (ws_v1 / ws_v2). Resolved at ingest by matching
    -- reports[].workspace_name to lib.config.WORKSPACES[].name, or via
    -- ?workspace_id= query override. Nullable if name is unknown.
    workspace_id              text,
    -- InboxAssure label from reports[].workspace_name (e.g. "Ottit V2")
    workspace_name            text,

    -- Full unwrapped webhook body for forward compatibility
    raw                       jsonb       not null default '{}'::jsonb,
    received_at               timestamptz not null default now(),
    updated_at                timestamptz not null default now()
);

create index if not exists idx_ia_spamchecks_status
    on v1.inboxassure_spamchecks (status);

create index if not exists idx_ia_spamchecks_ia_updated
    on v1.inboxassure_spamchecks (ia_updated_at desc nulls last);

create index if not exists idx_ia_spamchecks_workspace
    on v1.inboxassure_spamchecks (workspace_id);

-- Child: one row per account report inside the spamcheck
create table if not exists v1.inboxassure_spamcheck_reports (
    -- InboxAssure reports[].id (UUID string)
    id                        uuid        primary key,
    ia_spamcheck_id           bigint      not null
        references v1.inboxassure_spamchecks (ia_spamcheck_id)
        on delete cascade,
    email_account             text        not null,
    google_pro_score          numeric,
    outlook_pro_score         numeric,
    is_good                   boolean,
    sending_limit             integer,
    tags_list                 text,
    workspace_name            text,
    bounced_count             integer,
    unique_replied_count      integer,
    emails_sent_count         integer,
    ia_created_at             timestamptz,
    received_at               timestamptz not null default now(),
    updated_at                timestamptz not null default now()
);

create index if not exists idx_ia_spamcheck_reports_spamcheck
    on v1.inboxassure_spamcheck_reports (ia_spamcheck_id);

create index if not exists idx_ia_spamcheck_reports_email
    on v1.inboxassure_spamcheck_reports (email_account);

create index if not exists idx_ia_spamcheck_reports_is_good
    on v1.inboxassure_spamcheck_reports (is_good);

create index if not exists idx_ia_spamcheck_reports_workspace_name
    on v1.inboxassure_spamcheck_reports (workspace_name);

comment on table v1.inboxassure_spamchecks is
    'InboxAssure spamcheck.completed runs (n8n webhook). Upsert on ia_spamcheck_id.';

comment on table v1.inboxassure_spamcheck_reports is
    'Per-account rows for an InboxAssure spamcheck. Upsert on id (UUID).';
