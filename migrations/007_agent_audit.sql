-- ============================================================
-- Ottit Drafter — Migration 007: agent_audit table
-- Append-only audit log of every AI agent action. Skip if a row
-- with this table already exists in your Supabase project.
--
-- Schema: v1. The live project's `agent_audit` already lives there;
-- this file is the source-of-truth for fresh installs.
-- ============================================================

create schema if not exists v1;

create table if not exists v1.agent_audit (
    id            bigserial primary key,
    action        text not null,
    target_type   text not null,
    target_id     text not null,
    target_email  text,
    rule          text,
    new_value     jsonb,
    metadata      jsonb,
    created_at    timestamptz not null default now()
);

create index if not exists idx_agent_audit_target     on v1.agent_audit(target_type, target_id);
create index if not exists idx_agent_audit_created_at on v1.agent_audit(created_at desc);
create index if not exists idx_agent_audit_action     on v1.agent_audit(action);

comment on table v1.agent_audit is
  'Append-only audit trail of AI agent actions (draft.created, draft.sent, etc.).';
