-- ============================================================
-- Ottit CRM — Migration 003: domain_blacklist_checks table
-- Stores EmailGuard domain blacklist check results.
-- Run in Supabase SQL editor or via psql.
-- ============================================================

create table if not exists domain_blacklist_checks (
    id              bigserial primary key,
    eg_check_uuid   text not null unique,
    domain          text not null,
    ip              text,
    type            text,
    status          text,
    blacklists_count integer not null default 0,
    blacklists      text[] not null default '{}',
    last_polled_at  timestamptz not null default now(),
    created_at      timestamptz not null default now()
);

create index if not exists idx_dbc_domain
    on domain_blacklist_checks(domain);

create index if not exists idx_dbc_blacklisted
    on domain_blacklist_checks(blacklists_count)
    where blacklists_count > 0;

create index if not exists idx_dbc_last_polled
    on domain_blacklist_checks(last_polled_at desc);
