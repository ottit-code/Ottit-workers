-- ============================================================
-- Ottit CRM — Migration: Add notifications table
-- The following tables already exist in your Supabase project:
--   sender_daily_stats, domain_placement_tests, placement_test_emails,
--   spam_filter_tests, surbl_checks, sender_recovery, dashboard_action_log
-- Only run this migration to add the notifications table.
-- ============================================================

create table if not exists notifications (
  id bigserial primary key,
  severity text not null check (severity in ('critical', 'warning', 'info', 'resolved')),
  type text not null,
  entity_type text,
  entity_id text,
  title text not null,
  body text,
  read boolean default false,
  created_at timestamptz default now()
);

create index if not exists idx_notifications_created on notifications(created_at desc);
create index if not exists idx_notifications_unread on notifications(read) where read = false;

-- Enable realtime on notifications table:
-- ALTER PUBLICATION supabase_realtime ADD TABLE notifications;
