-- ============================================================
-- Ottit CRM — Migration 011: Placement test schedules
-- Recurring inbox-placement tests: the scheduler triggers due
-- rows via EmailGuard and advances next_run_at by the cadence.
-- Run in Supabase SQL editor or via psql.
-- ============================================================

create table if not exists placement_test_schedules (
    id bigint generated always as identity primary key,
    workspace_id text not null default 'ws_v1',
    -- Scope: at least one of domain / sender_email must be set.
    domain text,
    sender_email text,
    cadence text not null default 'weekly'
        check (cadence in ('daily', 'weekly', 'monthly')),
    enabled boolean not null default true,
    next_run_at timestamptz not null,
    last_run_at timestamptz,
    last_test_uuid text,
    last_error text,
    created_by text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint placement_test_schedules_scope_check
        check (domain is not null or sender_email is not null)
);

create index if not exists idx_placement_test_schedules_due
    on placement_test_schedules (next_run_at)
    where enabled;

create index if not exists idx_placement_test_schedules_domain
    on placement_test_schedules (domain);
