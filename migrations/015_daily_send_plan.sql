-- 015_daily_send_plan.sql
--
-- Midnight snapshot of each day's sending plan. The live Bison
-- scheduled-emails queue drains as emails are sent, so mid-day reads only
-- show what's *remaining*. send_plan_snapshotter captures the full plan
-- right after UTC midnight; /schedule/today then reports
-- sent / remaining / planned.

create table if not exists v1.daily_send_plan (
    workspace_id  text        not null,
    plan_date     date        not null,
    campaign_id   text        not null,
    campaign_name text,
    planned       integer     not null default 0,
    inboxes       jsonb,
    captured_at   timestamptz not null default now(),
    primary key (workspace_id, plan_date, campaign_id)
);

create index if not exists idx_daily_send_plan_date
    on v1.daily_send_plan (plan_date);
