-- 014_workspace_daily_stats_pk.sql
--
-- Migration 010 rebuilt the UNIQUE constraints on the multi-workspace tables
-- but only dropped contype='u' constraints. workspace_daily_stats keeps its
-- PRIMARY KEY on (stat_date) alone, so the first ws_v2 row for a date that
-- ws_v1 already has fails with:
--   duplicate key value violates unique constraint "workspace_daily_stats_pkey"
--
-- Replace the PK with a composite (workspace_id, stat_date). Idempotent.

do $$
declare
    pk_name text;
    pk_cols text[];
begin
    select c.conname,
           array_agg(a.attname order by k.ord)
      into pk_name, pk_cols
      from pg_constraint c
      join lateral unnest(c.conkey) with ordinality as k(attnum, ord) on true
      join pg_attribute a on a.attrelid = c.conrelid and a.attnum = k.attnum
     where c.conrelid = 'v1.workspace_daily_stats'::regclass
       and c.contype = 'p'
     group by c.conname;

    -- Already composite? Nothing to do.
    if pk_cols = array['workspace_id', 'stat_date']::text[] then
        return;
    end if;

    if pk_name is not null then
        execute format('alter table v1.workspace_daily_stats drop constraint %I', pk_name);
    end if;

    alter table v1.workspace_daily_stats
        add constraint workspace_daily_stats_pkey primary key (workspace_id, stat_date);
end $$;

-- The standalone unique index from 010 is now redundant with the PK.
drop index if exists v1.uq_wds_ws_date;
