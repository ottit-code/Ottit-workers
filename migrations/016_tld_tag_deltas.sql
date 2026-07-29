-- ============================================================
-- Ottit CRM — Migration 016: Delta-based TLD / tag performance
--
-- sender_daily_stats counters are CUMULATIVE lifetime snapshots from Bison.
-- The previous RPCs summed the raw values per day, so "Sent" for a TLD over
-- a week was the sum of every sender's lifetime total on each of 7 days
-- (e.g. 173k for .co) — and reply rates rounded to 0.0%.
--
-- These versions compute per-sender day-over-day deltas first (clamped >= 0;
-- a sender's first-ever snapshot reports 0 since its true daily value is
-- unknown), then aggregate. A 40-day lookback before p_start supplies the
-- baseline for deltas at the start of the range.
--
-- Also fixes cross-workspace collisions: sender counts dedupe on
-- (workspace_id, sender_email_id), and the tag join carries workspace_id.
--
-- Function names are schema-qualified: the `set search_path` clause only
-- applies INSIDE function execution, not to where CREATE FUNCTION puts the
-- function — an unqualified name in a session without v1 in its search_path
-- silently creates a stray copy in public (which happened on first apply;
-- fixed by 016b in the live DB).
-- ============================================================

-- Clean up stray public copies if a previous unqualified apply created them.
drop function if exists public.get_tld_daily_performance(date, date, text);
drop function if exists public.get_tag_daily_performance(date, date, text);

create or replace function v1.get_tld_daily_performance(
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
    with deltas as (
        select
            s.workspace_id,
            s.sender_email_id,
            s.domain,
            s.stat_date,
            s.daily_limit,
            case when lag(s.emails_sent) over w is null then 0
                 else greatest(0, s.emails_sent - lag(s.emails_sent) over w) end as d_sent,
            case when lag(s.emails_replied) over w is null then 0
                 else greatest(0, s.emails_replied - lag(s.emails_replied) over w) end as d_replied,
            case when lag(s.emails_bounced) over w is null then 0
                 else greatest(0, s.emails_bounced - lag(s.emails_bounced) over w) end as d_bounced,
            case when lag(s.warmup_sent) over w is null then 0
                 else greatest(0, s.warmup_sent - lag(s.warmup_sent) over w) end as d_warmup
        from sender_daily_stats s
        where s.stat_date between p_start - 40 and p_end
          and s.domain is not null
          and position('.' in s.domain) > 0
          and (p_workspace_id is null or s.workspace_id = p_workspace_id)
        window w as (partition by s.workspace_id, s.sender_email_id order by s.stat_date)
    )
    select
        lower(substring(d.domain from '\.[^.]+$')) as tld,
        d.stat_date,
        count(distinct (d.workspace_id, d.sender_email_id)) as senders,
        coalesce(sum(d.d_sent), 0) as emails_sent,
        coalesce(sum(d.d_replied), 0) as emails_replied,
        coalesce(sum(d.d_bounced), 0) as emails_bounced,
        coalesce(sum(d.d_warmup), 0) as warmup_sent,
        coalesce(sum(d.daily_limit), 0) as daily_limit
    from deltas d
    where d.stat_date between p_start and p_end
    group by 1, 2
    order by 1, 2;
$$;

create or replace function v1.get_tag_daily_performance(
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
        select distinct on (workspace_id, sender_email_id)
            workspace_id, sender_email_id, tags
        from sender_email_performance
        where (p_workspace_id is null or workspace_id = p_workspace_id)
        order by workspace_id, sender_email_id, snapshot_date desc
    ),
    sender_tags as (
        select
            lp.workspace_id,
            lp.sender_email_id,
            coalesce(
                nullif(elem->>'name', ''),
                case when jsonb_typeof(elem) = 'string' then elem #>> '{}' end
            ) as tag
        from latest_perf lp
        cross join lateral jsonb_array_elements(
            coalesce(to_jsonb(lp.tags), '[]'::jsonb)
        ) as elem
    ),
    deltas as (
        select
            s.workspace_id,
            s.sender_email_id,
            s.stat_date,
            s.daily_limit,
            case when lag(s.emails_sent) over w is null then 0
                 else greatest(0, s.emails_sent - lag(s.emails_sent) over w) end as d_sent,
            case when lag(s.emails_replied) over w is null then 0
                 else greatest(0, s.emails_replied - lag(s.emails_replied) over w) end as d_replied,
            case when lag(s.emails_bounced) over w is null then 0
                 else greatest(0, s.emails_bounced - lag(s.emails_bounced) over w) end as d_bounced,
            case when lag(s.warmup_sent) over w is null then 0
                 else greatest(0, s.warmup_sent - lag(s.warmup_sent) over w) end as d_warmup
        from sender_daily_stats s
        where s.stat_date between p_start - 40 and p_end
          and (p_workspace_id is null or s.workspace_id = p_workspace_id)
        window w as (partition by s.workspace_id, s.sender_email_id order by s.stat_date)
    )
    select
        st.tag,
        d.stat_date,
        count(distinct (d.workspace_id, d.sender_email_id)) as senders,
        coalesce(sum(d.d_sent), 0) as emails_sent,
        coalesce(sum(d.d_replied), 0) as emails_replied,
        coalesce(sum(d.d_bounced), 0) as emails_bounced,
        coalesce(sum(d.d_warmup), 0) as warmup_sent,
        coalesce(sum(d.daily_limit), 0) as daily_limit
    from deltas d
    join sender_tags st
      on st.workspace_id = d.workspace_id
     and st.sender_email_id = d.sender_email_id
    where d.stat_date between p_start and p_end
      and st.tag is not null
    group by st.tag, d.stat_date
    order by st.tag, d.stat_date;
$$;
