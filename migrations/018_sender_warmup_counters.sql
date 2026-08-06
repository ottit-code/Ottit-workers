-- ============================================================
-- Ottit CRM — Migration 018: Warmup counters on performance
--
-- Persist EmailBison /api/warmup/sender-emails fields onto the
-- daily sender_email_performance snapshot so GET /warmup/report
-- can show warmup_sent, spam saves, and bounce counters (bison-
-- reports parity) without re-calling Bison for historical dates.
-- ============================================================

alter table v1.sender_email_performance
    add column if not exists warmup_sent integer,
    add column if not exists warmup_replied integer,
    add column if not exists warmup_saved_from_spam integer,
    add column if not exists warmup_bounces_received integer,
    add column if not exists warmup_bounces_caused integer;
