# Prompt: Apply Supabase migration 018 — Warmup counters on performance

Copy everything below the line into Claude / Codex (or run the SQL yourself in the Supabase SQL Editor).

---

## Goal / context

Apply **migration 018** for Ottit Warmup Report bison-reports parity so daily `sender_email_performance` snapshots store EmailBison warmup counters (sent, replies, spam saves, bounces).

**Product:** Ottit dashboard Warmup Report account lists and never-warmed detection need `warmup_sent` / spam saves / bounce counters on historical performance rows — without re-calling Bison for past dates.

**Code already shipped** (ottit-workers):

- `migrations/018_sender_warmup_counters.sql` — adds five nullable integer columns on `v1.sender_email_performance`
- `workers/sender_performance_poller.py` — maps `/api/warmup/sender-emails` → columns; upserts with fallback if columns missing
- `lib/warmup_report.py` — selects counters for report lists / never_warmed; legacy select if columns missing
- API: `GET /warmup/report`, `GET /warmup/correlation` (`api/routers/aggregates.py`)
- Commit: `7a4fc9d` (Persist Bison warmup counters and add warmup↔reply correlation API)

**Schema:** All worker tables live in Postgres schema **`v1`** (not `public`). The Supabase Python client is configured with `ClientOptions(schema="v1")` in `lib/supabase_client.py`, so `.table("sender_email_performance")` resolves to `v1.sender_email_performance`.

**Do not** invent extra columns, indexes, RLS, or RPCs. Apply exactly the SQL below. Idempotent (`IF NOT EXISTS`). Existing rows keep `NULL` in the new columns until the next poller write.

**Prerequisite:** `v1.sender_email_performance` already exists (long-lived table). Migration **017** (`warmup_daily_report`) is independent — apply 017 for fleet snapshots; apply **018** for per-sender counter columns on performance.

**Do not confuse** with `v1.sender_daily_stats.warmup_sent` / `warmup_replied` (stats poller / tag-performance RPCs). This migration only touches **`sender_email_performance`**.

---

## Exact SQL to run

Paste and run this entire script in the Supabase SQL Editor:

```sql
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
```

### Column reference (for the agent applying this)

| Column | Type | Nullable | Bison source field (`/api/warmup/sender-emails`) | Role |
|--------|------|----------|--------------------------------------------------|------|
| `warmup_sent` | integer | yes | `warmup_emails_sent` | Cumulative warmup emails sent (never_warmed when `0`) |
| `warmup_replied` | integer | yes | `warmup_replies_received` | Warmup replies received |
| `warmup_saved_from_spam` | integer | yes | `warmup_emails_saved_from_spam` | Spam saves (report `spam_saves`) |
| `warmup_bounces_received` | integer | yes | `warmup_bounces_received_count` | Warmup bounces received |
| `warmup_bounces_caused` | integer | yes | `warmup_bounces_caused_count` | Warmup bounces caused |

All five are nullable: `NULL` means unknown / not yet written (pre-migration rows or Bison omitted the field). App coerces with `_int_counter` (None stays None).

No new indexes or PK changes. Upsert conflict target unchanged: `on_conflict="workspace_id,sender_email_id,snapshot_date"`.

---

## Where to run it (Supabase SQL Editor)

1. Open the **Ottit Supabase project** (the one `SUPABASE_URL` / service key point at).
2. Go to **SQL Editor** → **New query**.
3. Paste the full SQL block above.
4. Confirm the script targets schema **`v1`** (`alter table v1.sender_email_performance`, not `public.*`).
5. Click **Run**. Expect success with no errors; re-running is safe (`IF NOT EXISTS`).
6. Optionally save the query as `018_sender_warmup_counters` for audit trail.

No CLI / supabase migration runner is required unless you already use one for this project — the authoritative file in-repo is:

`migrations/018_sender_warmup_counters.sql`

---

## Verification queries (run after apply)

```sql
-- 1) Columns exist on v1.sender_email_performance
select column_name, data_type, is_nullable, column_default
from information_schema.columns
where table_schema = 'v1'
  and table_name = 'sender_email_performance'
  and column_name in (
    'warmup_sent',
    'warmup_replied',
    'warmup_saved_from_spam',
    'warmup_bounces_received',
    'warmup_bounces_caused'
  )
order by column_name;

-- Expect 5 rows, all data_type = integer, is_nullable = YES

-- 2) Spot-check: existing rows are NULL until next poller run (OK)
select
  count(*) as total_rows,
  count(warmup_sent) as rows_with_warmup_sent,
  count(warmup_saved_from_spam) as rows_with_spam_saves
from v1.sender_email_performance;

-- 3) After next sender_performance_poller run (~1 AM) or a manual poll:
select
  workspace_id,
  sender_email,
  snapshot_date,
  warmup_score,
  warmup_sent,
  warmup_replied,
  warmup_saved_from_spam,
  warmup_bounces_received,
  warmup_bounces_caused
from v1.sender_email_performance
where snapshot_date = current_date
order by workspace_id, sender_email
limit 20;
```

**App-level smoke (after columns exist + next poll):**

- Logs should **not** show `Upserted performance without warmup counters (apply migration 018)`.
- `GET /warmup/report` account lists (`below_threshold`, `not_warming_accounts`, `never_warmed_accounts`) should include `warmup_sent`, `spam_saves`, `bounces_caused`, `bounces_received` when Bison returned them.
- `never_warmed` bucket uses `warmup_sent == 0` (not only `score == 0` fallback).
- `GET /warmup/correlation` can use persisted counters once rows are written with non-null values.

---

## What still works without this migration vs what breaks

### Still works (columns missing — code fallbacks)

- **Core `sender_email_performance` upserts** — poller catches missing-column errors, strips the five counter keys, retries upsert, logs a warning. Performance / health / tags / recovery fields still write.
- **Warmup Report buckets / live report** — `fetch_performance_rows` falls back to `_PERF_COLS_LEGACY` (no counter columns). Score bands, not_warming, not_connected still work.
- **`never_warmed` (degraded)** — falls back to `warmup_score == 0` when `warmup_sent` is unavailable (less accurate than bison-reports `warmup_sent == 0`).
- **Fleet snapshot `warmup_daily_report` (017)** — poller still calls `persist_warmup_daily_report` with **in-memory** rows that include counters, so today’s JSON `payload` account lists can still carry counters even if DB columns are missing. Historical recompute from DB performance rows will lack counters.
- **`sender_daily_stats` warmup fields** — unrelated; stats poller continues as before.
- **Migration 017 table** — independent; not required for 018 and vice versa.

### Breaks / degraded until migration is applied

- **Persisted counters on performance rows** — columns missing → no durable `warmup_sent` / spam / bounce history on `sender_email_performance` for past dates.
- **Historical report recompute from DB** — `GET /warmup/report?date=...` when loading from performance (no rich snapshot / live path) cannot show per-account warmup counters; account list fields stay null.
- **Accurate never_warmed** — without `warmup_sent`, enabled accounts with score &gt; 0 but zero warmup sends may be mis-bucketed as score bands instead of never_warmed.
- **Poller warning noise** — repeated `Upserted performance without warmup counters (apply migration 018)` until columns exist.
- **Correlation / charts that need DB counters over many days** — incomplete until columns exist and poller has written several days of non-null values (past days stay NULL unless backfilled).

**No backfill SQL is included** — counters come from live Bison on each poll. Pre-018 rows remain NULL unless you re-run a performance poll for those dates.

---

## Rollback SQL (if needed)

Only if you must undo this migration. Drops the five columns and any values written after apply:

```sql
alter table v1.sender_email_performance
    drop column if exists warmup_sent,
    drop column if exists warmup_replied,
    drop column if exists warmup_saved_from_spam,
    drop column if exists warmup_bounces_received,
    drop column if exists warmup_bounces_caused;
```

After rollback, behavior returns to the “still works without migration” state above (legacy select + stripped upsert). Does not touch `warmup_daily_report`, `sender_daily_stats`, or other tables.

---

## Safety notes

- **Schema is `v1`** — do not alter `public.sender_email_performance` (if it even exists).
- **Idempotent** — `ADD COLUMN IF NOT EXISTS`; safe to re-run.
- **No data loss on apply** — additive only; existing rows get NULL for new columns.
- **Nullable integers** — no defaults; app treats NULL as unknown.
- **No RLS / grants in this migration** — matches other worker tables; workers use the **service role** key against schema `v1`.
- **Not the same as `sender_daily_stats.warmup_sent`** — do not “fix” by altering the wrong table.
- **Rollback drops data** in those five columns only.

---

## Done when

1. Verification query (1) returns all five integer columns on `v1.sender_email_performance`.
2. Next successful `poll_sender_email_performance` writes non-null counters for senders Bison returned (spot-check query 3).
3. Warmup Report account lists show `warmup_sent` / `spam_saves` / bounce fields; poller no longer logs the migration-018 fallback warning.
