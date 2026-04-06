-- ============================================================
-- Ottit CRM — Initial Supabase Schema
-- Run this in Supabase SQL Editor to set up all required tables
-- ============================================================

-- Sender daily stats (time-series, written by stats_poller)
create table if not exists sender_daily_stats (
  id uuid primary key default gen_random_uuid(),
  sender_email_id text not null,
  sender_email text not null,
  domain text not null,
  stat_date date not null,
  emails_sent int default 0,
  emails_bounced int default 0,
  daily_limit int default 0,
  warmup_enabled boolean default false,
  created_at timestamptz default now(),
  unique(sender_email_id, stat_date)
);

-- Inbox placement tests (written by delivery_poller)
create table if not exists domain_placement_tests (
  id uuid primary key default gen_random_uuid(),
  external_uuid text unique not null,
  domain text,
  status text,  -- pending | completed | failed
  created_at timestamptz default now(),
  completed_at timestamptz
);

create table if not exists placement_test_emails (
  id uuid primary key default gen_random_uuid(),
  test_id uuid references domain_placement_tests(id) on delete cascade,
  provider text,
  inbox boolean,
  spam boolean,
  promotions boolean,
  raw jsonb
);

-- Spam filter tests (written by delivery_poller)
create table if not exists spam_filter_tests (
  id uuid primary key default gen_random_uuid(),
  external_uuid text unique not null,
  domain text,
  score float,
  status text,
  raw jsonb,
  created_at timestamptz default now()
);

-- SURBL blacklist checks (written by delivery_poller)
create table if not exists surbl_checks (
  id uuid primary key default gen_random_uuid(),
  domain text not null,
  listed boolean,
  details jsonb,
  checked_at timestamptz default now()
);

-- Notifications (written by notifier, read by dashboard with realtime)
create table if not exists notifications (
  id uuid primary key default gen_random_uuid(),
  severity text not null check (severity in ('critical', 'warning', 'info', 'resolved')),
  type text not null,
  entity_type text,
  entity_id text,
  title text not null,
  body text,
  read boolean default false,
  created_at timestamptz default now()
);

-- Enable realtime on notifications table (run separately in Supabase dashboard
-- or via: ALTER PUBLICATION supabase_realtime ADD TABLE notifications;)

-- Indexes for common query patterns
create index if not exists idx_sender_daily_stats_date on sender_daily_stats(stat_date desc);
create index if not exists idx_sender_daily_stats_email on sender_daily_stats(sender_email_id);
create index if not exists idx_notifications_created on notifications(created_at desc);
create index if not exists idx_notifications_read on notifications(read) where read = false;
