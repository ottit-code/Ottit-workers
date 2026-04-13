-- ============================================================
-- Ottit CRM — Migration 002: Indexes + get_latest_sender_stats RPC
-- Run in Supabase SQL editor or via psql.
-- ============================================================

-- -------- sender_daily_stats --------

-- Fast lookup for "today's snapshot" (used by /senders and notifier)
create index if not exists idx_sds_stat_date
    on sender_daily_stats(stat_date desc);

-- Fast lookup for per-sender history (used by /senders/{id}/history)
create index if not exists idx_sds_sender_date
    on sender_daily_stats(sender_email_id, stat_date desc);

-- -------- notifications --------

-- Fast dedup check in notifier: type + entity_id + date
create index if not exists idx_notif_type_entity_date
    on notifications(type, entity_id, created_at desc);

-- -------- spam_filter_tests --------

-- Used by notifier (filter by created_at today) and API list endpoint
create index if not exists idx_sft_created
    on spam_filter_tests(created_at desc);

-- -------- surbl_checks --------

-- Used by API list endpoint filtered by domain
create index if not exists idx_surbl_domain_created
    on surbl_checks(domain, created_at desc);

-- ============================================================
-- RPC: get_latest_sender_stats
-- Returns the most recent stat row per sender (DISTINCT ON).
-- Used by the /senders fallback path in api/main.py.
-- ============================================================

create or replace function get_latest_sender_stats(
    p_domain text default null,
    p_warmup_enabled boolean default null
)
returns setof sender_daily_stats
language sql
stable
as $$
    select distinct on (sender_email_id) *
    from sender_daily_stats
    where (p_domain is null or domain = p_domain)
      and (p_warmup_enabled is null or warmup_enabled = p_warmup_enabled)
    order by sender_email_id, stat_date desc;
$$;
