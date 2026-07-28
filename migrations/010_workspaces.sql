-- ============================================================
-- Ottit CRM — Migration 010: Multi-workspace support
-- Adds workspace_id to snapshot tables (default 'ws_v1' backfills
-- existing rows), rebuilds per-workspace upsert conflict keys, and
-- makes the sender-stats RPCs workspace-aware.
-- Run in Supabase SQL editor or via psql.
-- ============================================================

-- -------- workspace_id columns (default backfills existing rows) --------

alter table sender_daily_stats
    add column if not exists workspace_id text not null default 'ws_v1';
alter table workspace_daily_stats
    add column if not exists workspace_id text not null default 'ws_v1';
alter table campaign_daily_stats
    add column if not exists workspace_id text not null default 'ws_v1';
alter table sender_email_performance
    add column if not exists workspace_id text not null default 'ws_v1';
alter table domain_placement_tests
    add column if not exists workspace_id text not null default 'ws_v1';
alter table reply_events
    add column if not exists workspace_id text not null default 'ws_v1';
alter table lead_engagement_snapshots
    add column if not exists workspace_id text not null default 'ws_v1';

create index if not exists idx_sds_workspace on sender_daily_stats(workspace_id);
create index if not exists idx_wds_workspace on workspace_daily_stats(workspace_id);
create index if not exists idx_cds_workspace on campaign_daily_stats(workspace_id);
create index if not exists idx_sep_workspace on sender_email_performance(workspace_id);

-- -------- rebuild upsert conflict keys to include workspace_id --------
-- Two Bison workspaces can reuse the same sender/campaign ids, so the old
-- unique keys would collide across workspaces. Drop any UNIQUE constraint or
-- unique index on the old column sets (names are unknown — these tables were
-- created outside repo migrations) and create workspace-scoped replacements.

do $$
declare
    rec record;
begin
    for rec in
        select c.conname, t.relname
        from pg_constraint c
        join pg_class t on t.oid = c.conrelid
        where c.contype = 'u'
          and (
            (t.relname = 'sender_daily_stats'
             and c.conkey::int[] <@ (
                select array_agg(attnum)::int[]
                from pg_attribute
                where attrelid = t.oid and attname in ('sender_email_id', 'stat_date')))
            or
            (t.relname = 'workspace_daily_stats'
             and c.conkey::int[] <@ (
                select array_agg(attnum)::int[]
                from pg_attribute
                where attrelid = t.oid and attname in ('stat_date')))
            or
            (t.relname = 'campaign_daily_stats'
             and c.conkey::int[] <@ (
                select array_agg(attnum)::int[]
                from pg_attribute
                where attrelid = t.oid and attname in ('campaign_id', 'stat_date')))
            or
            (t.relname = 'sender_email_performance'
             and c.conkey::int[] <@ (
                select array_agg(attnum)::int[]
                from pg_attribute
                where attrelid = t.oid and attname in ('sender_email_id', 'snapshot_date')))
          )
    loop
        execute format('alter table %I drop constraint %I', rec.relname, rec.conname);
    end loop;

    -- Standalone unique indexes (not backed by a constraint) on the same
    -- column sets also have to go.
    for rec in
        select i.relname as idxname
        from pg_index x
        join pg_class i on i.oid = x.indexrelid
        join pg_class t on t.oid = x.indrelid
        where x.indisunique
          and not x.indisprimary
          and not exists (select 1 from pg_constraint c where c.conindid = i.oid)
          and (
            (t.relname = 'sender_daily_stats'
             and string_to_array(x.indkey::text, ' ')::int[] <@ (
                select array_agg(attnum)::int[]
                from pg_attribute
                where attrelid = t.oid and attname in ('sender_email_id', 'stat_date')))
            or
            (t.relname = 'workspace_daily_stats'
             and string_to_array(x.indkey::text, ' ')::int[] <@ (
                select array_agg(attnum)::int[]
                from pg_attribute
                where attrelid = t.oid and attname in ('stat_date')))
            or
            (t.relname = 'campaign_daily_stats'
             and string_to_array(x.indkey::text, ' ')::int[] <@ (
                select array_agg(attnum)::int[]
                from pg_attribute
                where attrelid = t.oid and attname in ('campaign_id', 'stat_date')))
            or
            (t.relname = 'sender_email_performance'
             and string_to_array(x.indkey::text, ' ')::int[] <@ (
                select array_agg(attnum)::int[]
                from pg_attribute
                where attrelid = t.oid and attname in ('sender_email_id', 'snapshot_date')))
          )
    loop
        execute format('drop index %I', rec.idxname);
    end loop;
end $$;

create unique index if not exists uq_sds_ws_sender_date
    on sender_daily_stats(workspace_id, sender_email_id, stat_date);
create unique index if not exists uq_wds_ws_date
    on workspace_daily_stats(workspace_id, stat_date);
create unique index if not exists uq_cds_ws_campaign_date
    on campaign_daily_stats(workspace_id, campaign_id, stat_date);
create unique index if not exists uq_sep_ws_sender_date
    on sender_email_performance(workspace_id, sender_email_id, snapshot_date);

-- -------- workspace-aware RPCs --------

drop function if exists get_latest_sender_stats(text, boolean);

create or replace function get_latest_sender_stats(
    p_domain text default null,
    p_warmup_enabled boolean default null,
    p_workspace_id text default null
)
returns setof sender_daily_stats
language sql
stable
set search_path = v1, public
as $$
    select distinct on (workspace_id, sender_email_id) *
    from sender_daily_stats
    where (p_domain is null or domain = p_domain)
      and (p_warmup_enabled is null or warmup_enabled = p_warmup_enabled)
      and (p_workspace_id is null or workspace_id = p_workspace_id)
    order by workspace_id, sender_email_id, stat_date desc;
$$;

drop function if exists get_tld_daily_performance(date, date);

create or replace function get_tld_daily_performance(
    p_start date,
    p_end date,
    p_workspace_id text default null
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
      and (p_workspace_id is null or workspace_id = p_workspace_id)
    group by 1, 2
    order by 1, 2;
$$;

drop function if exists get_tag_daily_performance(date, date);

create or replace function get_tag_daily_performance(
    p_start date,
    p_end date,
    p_workspace_id text default null
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
        select distinct on (workspace_id, sender_email_id) sender_email_id, tags
        from sender_email_performance
        where (p_workspace_id is null or workspace_id = p_workspace_id)
        order by workspace_id, sender_email_id, snapshot_date desc
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
      and (p_workspace_id is null or s.workspace_id = p_workspace_id)
    group by st.tag, s.stat_date
    order by st.tag, s.stat_date;
$$;
