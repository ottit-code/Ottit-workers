-- ============================================================
-- Ottit CRM — Migration 012: Cohort reply rate
-- Reply rate for a range = emails sent in that range and the replies
-- those emails received, whenever the replies landed. Replies are
-- attributed to the ORIGINAL email's sent date (reply_events.original_sent_at).
--
-- Also scopes the reply_events unique key by workspace (two Bison
-- workspaces can reuse the same reply ids).
-- Run in Supabase SQL editor (v1 schema) or via psql.
-- ============================================================

-- -------- workspace-scoped unique key on reply_events --------

do $$
declare
    rec record;
begin
    for rec in
        select c.conname
        from pg_constraint c
        join pg_class t on t.oid = c.conrelid
        where c.contype = 'u'
          and t.relname = 'reply_events'
          and c.conkey::int[] <@ (
              select array_agg(attnum)::int[]
              from pg_attribute
              where attrelid = t.oid and attname in ('reply_id'))
    loop
        execute format('alter table reply_events drop constraint %I', rec.conname);
    end loop;

    for rec in
        select i.relname as idxname
        from pg_index x
        join pg_class i on i.oid = x.indexrelid
        join pg_class t on t.oid = x.indrelid
        where x.indisunique
          and not x.indisprimary
          and t.relname = 'reply_events'
          and not exists (select 1 from pg_constraint c where c.conindid = i.oid)
          and string_to_array(x.indkey::text, ' ')::int[] <@ (
              select array_agg(attnum)::int[]
              from pg_attribute
              where attrelid = t.oid and attname in ('reply_id'))
    loop
        execute format('drop index %I', rec.idxname);
    end loop;
end $$;

create unique index if not exists uq_re_ws_reply
    on reply_events(workspace_id, reply_id);

create index if not exists idx_re_original_sent_at
    on reply_events(original_sent_at);
create index if not exists idx_re_workspace
    on reply_events(workspace_id);

-- -------- cohort reply counts RPC --------
-- Groups non-automated replies by the ORIGINAL email's sent date into
-- campaign / sender / TLD / tag buckets.

drop function if exists get_cohort_reply_counts(date, date, text, text);

create or replace function get_cohort_reply_counts(
    p_start date,
    p_end date,
    p_group text,               -- 'campaign' | 'sender' | 'tld' | 'tag'
    p_workspace_id text default null
)
returns table (
    group_key text,
    stat_date date,
    cohort_replies bigint
)
language sql
stable
set search_path = v1, public
as $$
    with base as (
        select
            r.campaign_id,
            r.sender_email_id,
            lower(r.sender_email) as sender_email,
            lower(substring(split_part(coalesce(r.sender_email, ''), '@', 2) from '\.[^.]+$')) as tld,
            (r.original_sent_at at time zone 'UTC')::date as sent_date
        from reply_events r
        where r.original_sent_at is not null
          and (r.original_sent_at at time zone 'UTC')::date between p_start and p_end
          and r.classification is distinct from 'automated_reply'
          and (p_workspace_id is null or r.workspace_id = p_workspace_id)
    ),
    latest_perf as (
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
    select b.campaign_id as group_key, b.sent_date as stat_date, count(*)::bigint
    from base b
    where p_group = 'campaign' and b.campaign_id is not null
    group by 1, 2

    union all

    select b.sender_email, b.sent_date, count(*)::bigint
    from base b
    where p_group = 'sender' and b.sender_email is not null
    group by 1, 2

    union all

    select b.tld, b.sent_date, count(*)::bigint
    from base b
    where p_group = 'tld' and b.tld is not null
    group by 1, 2

    union all

    select st.tag, b.sent_date, count(*)::bigint
    from base b
    join sender_tags st on st.sender_email_id = b.sender_email_id
    where p_group = 'tag' and st.tag is not null
    group by 1, 2

    order by 1, 2;
$$;
