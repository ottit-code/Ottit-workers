-- ============================================================
-- Ottit CRM — Migration 009: TLD + Tag daily performance RPCs
-- Run in Supabase SQL editor or via psql.
-- ============================================================

-- ------------------------------------------------------------
-- RPC: get_tld_daily_performance
-- Per-TLD, per-day aggregates over sender_daily_stats.
-- TLD is derived from the last dot-segment of the sender domain
-- (e.g. mail.acme.com -> .com).
-- Used by GET /senders/tld-performance.
-- ------------------------------------------------------------

create or replace function get_tld_daily_performance(
    p_start date,
    p_end date
)
returns table (
    tld text,
    stat_date date,
    senders bigint,
    emails_sent bigint,
    emails_replied bigint,
    emails_bounced bigint,
    warmup_sent bigint,
    daily_limit bigint
)
language sql
stable
set search_path = v1, public
as $$
    select
        lower(substring(domain from '\.[^.]+$')) as tld,
        stat_date,
        count(distinct sender_email_id) as senders,
        coalesce(sum(emails_sent), 0) as emails_sent,
        coalesce(sum(emails_replied), 0) as emails_replied,
        coalesce(sum(emails_bounced), 0) as emails_bounced,
        coalesce(sum(warmup_sent), 0) as warmup_sent,
        coalesce(sum(daily_limit), 0) as daily_limit
    from sender_daily_stats
    where stat_date between p_start and p_end
      and domain is not null
      and position('.' in domain) > 0
    group by 1, 2
    order by 1, 2;
$$;

-- ------------------------------------------------------------
-- RPC: get_tag_daily_performance
-- Per-tag, per-day aggregates: joins sender_daily_stats to each
-- sender's latest tags from sender_email_performance.
-- Tags are Bison tag objects ({id, name, ...}) or plain strings;
-- to_jsonb() normalises json / jsonb / text[] storage.
-- Used by GET /senders/tag-performance.
-- ------------------------------------------------------------

create or replace function get_tag_daily_performance(
    p_start date,
    p_end date
)
returns table (
    tag text,
    stat_date date,
    senders bigint,
    emails_sent bigint,
    emails_replied bigint,
    emails_bounced bigint,
    warmup_sent bigint,
    daily_limit bigint
)
language sql
stable
set search_path = v1, public
as $$
    with latest_perf as (
        select distinct on (sender_email_id) sender_email_id, tags
        from sender_email_performance
        order by sender_email_id, snapshot_date desc
    ),
    sender_tags as (
        select
            lp.sender_email_id,
            coalesce(
                nullif(elem->>'name', ''),
                case when jsonb_typeof(elem) = 'string' then elem #>> '{}' end
            ) as tag
        from latest_perf lp
        cross join lateral jsonb_array_elements(
            coalesce(to_jsonb(lp.tags), '[]'::jsonb)
        ) as elem
    )
    select
        st.tag,
        s.stat_date,
        count(distinct s.sender_email_id) as senders,
        coalesce(sum(s.emails_sent), 0) as emails_sent,
        coalesce(sum(s.emails_replied), 0) as emails_replied,
        coalesce(sum(s.emails_bounced), 0) as emails_bounced,
        coalesce(sum(s.warmup_sent), 0) as warmup_sent,
        coalesce(sum(s.daily_limit), 0) as daily_limit
    from sender_daily_stats s
    join sender_tags st on st.sender_email_id = s.sender_email_id
    where s.stat_date between p_start and p_end
      and st.tag is not null
    group by st.tag, s.stat_date
    order by st.tag, s.stat_date;
$$;
