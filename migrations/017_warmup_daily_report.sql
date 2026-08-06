-- ============================================================
-- Ottit CRM — Migration 017: Warmup daily fleet report
--
-- Daily snapshot of Slack-style warmup buckets (≥95 / 90–94 / <90 /
-- not warming), plus a JSON payload for below-threshold accounts and
-- per-tag breakdowns. Written by sender_performance_poller; served by
-- GET /warmup/report for historical dates.
-- ============================================================

create table if not exists v1.warmup_daily_report (
    workspace_id     text        not null,
    report_date      date        not null,
    total_accounts   integer     not null default 0,
    not_warming      integer     not null default 0,
    score_95_plus    integer     not null default 0,
    score_90_to_94   integer     not null default 0,
    score_below_90   integer     not null default 0,
    payload          jsonb       not null default '{}'::jsonb,
    captured_at      timestamptz not null default now(),
    primary key (workspace_id, report_date)
);

create index if not exists idx_warmup_daily_report_date
    on v1.warmup_daily_report (report_date);
