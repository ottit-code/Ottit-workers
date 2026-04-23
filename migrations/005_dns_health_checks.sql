-- ============================================================
-- Ottit CRM — Migration 005: dns_health_checks table
-- Stores per-record SPF / DKIM / DMARC pass booleans produced by
-- workers/dns_check_poller.py. Surfaced via /deliverability/domain-health
-- (API joins latest row per domain onto the existing v_domain_health view).
-- Run in Supabase SQL editor or via psql.
-- ============================================================

create table if not exists dns_health_checks (
    id              bigserial primary key,
    domain          text not null,
    spf_passed      boolean,
    spf_record      text,
    dkim_passed     boolean,
    dkim_selector   text,
    dmarc_passed    boolean,
    dmarc_policy    text,
    checked_at      timestamptz not null default now()
);

create index if not exists idx_dhc_domain_checked
    on dns_health_checks(domain, checked_at desc);
