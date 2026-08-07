# Prompt: Apply Supabase migration 017 — Warmup daily report

Copy everything below the line into Claude / Codex (or run the SQL yourself in the Supabase SQL Editor).

---

## Goal / context

Apply **migration 017** for the Ottit Warmup Report feature so the dashboard can archive daily fleet warmup snapshots.

**Product:** Ottit dashboard Warmup Report (Slack / bison-reports style buckets: not warming, ≥95, 90–94, &lt;90, never warmed, etc.).

**Code already shipped** (ottit-workers):

- `migrations/017_warmup_daily_report.sql` — creates `v1.warmup_daily_report`
- `lib/warmup_report.py` — builds report, upserts snapshots, serves live vs historical
- `workers/sender_performance_poller.py` — after writing `sender_email_performance`, calls `persist_warmup_daily_report(...)`
- API: `GET /warmup/report` and `GET /warmup/report/dates` (`api/routers/aggregates.py`)
- Commits: `1815a87` (table + API + poller), `d3175d3` (deeper payload; **no extra columns** — still stored in `payload` jsonb)

**Schema:** All worker tables live in Postgres schema **`v1`** (not `public`). The Supabase Python client is configured with `ClientOptions(schema="v1")` in `lib/supabase_client.py`, so `.table("warmup_daily_report")` resolves to `v1.warmup_daily_report`.

**Do not** invent extra columns, RLS, or RPCs. Apply exactly the SQL below. Idempotent (`IF NOT EXISTS`). No existing data is modified.

---

## Exact SQL to run

Paste and run this entire script in the Supabase SQL Editor:

```sql
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
```

### Column reference (for the agent applying this)

| Column | Type | Role |
|--------|------|------|
| `workspace_id` | text | Workspace key (`v1` / `v2` / etc.); part of PK |
| `report_date` | date | Calendar day of snapshot; part of PK |
| `total_accounts` | int | Fleet size that day |
| `not_warming` | int | Warmup disabled / not warming bucket |
| `score_95_plus` | int | Health score ≥ 95 |
| `score_90_to_94` | int | Health score 90–94 |
| `score_below_90` | int | Health score &lt; 90 (scored accounts) |
| `payload` | jsonb | Lists + tags + stats: `below_threshold`, `not_warming_accounts`, `never_warmed_accounts`, `by_tag`, `percentages`, `stats`, `not_connected`, `never_warmed`, `score_below_95` |
| `captured_at` | timestamptz | When the snapshot was written |

Upsert conflict target used by the app: `on_conflict="workspace_id,report_date"` (matches the primary key).

---

## Where to run it (Supabase SQL Editor)

1. Open the **Ottit Supabase project** (the one `SUPABASE_URL` / service key point at).
2. Go to **SQL Editor** → **New query**.
3. Paste the full SQL block above.
4. Confirm the script targets schema **`v1`** (table name is `v1.warmup_daily_report`, not `public.*`).
5. Click **Run**. Expect success with no errors; re-running is safe (`IF NOT EXISTS`).
6. Optionally save the query as `017_warmup_daily_report` for audit trail.

No CLI / supabase migration runner is required unless you already use one for this project — the authoritative file in-repo is:

`migrations/017_warmup_daily_report.sql`

---

## Verification queries (run after apply)

```sql
-- 1) Table exists in v1
select table_schema, table_name
from information_schema.tables
where table_schema = 'v1'
  and table_name = 'warmup_daily_report';

-- 2) Columns + types
select column_name, data_type, is_nullable, column_default
from information_schema.columns
where table_schema = 'v1'
  and table_name = 'warmup_daily_report'
order by ordinal_position;

-- 3) Primary key
select tc.constraint_name, kcu.column_name
from information_schema.table_constraints tc
join information_schema.key_column_usage kcu
  on tc.constraint_name = kcu.constraint_name
 and tc.table_schema = kcu.table_schema
where tc.table_schema = 'v1'
  and tc.table_name = 'warmup_daily_report'
  and tc.constraint_type = 'PRIMARY KEY'
order by kcu.ordinal_position;

-- 4) Index present
select indexname, indexdef
from pg_indexes
where schemaname = 'v1'
  and tablename = 'warmup_daily_report';

-- 5) Empty is OK until the next sender_performance_poller run (~1 AM) or a manual backfill
select count(*) as row_count from v1.warmup_daily_report;
```

**App-level smoke (after table exists):**

- `GET /warmup/report` (no date / today) — should still return `source: "live"` from `sender_email_performance`.
- After poller runs (or after a one-off `persist_warmup_daily_report` for today’s workspace):  
  `select * from v1.warmup_daily_report where report_date = current_date;`
- `GET /warmup/report/dates` — should include snapshot headline fields when rows exist.
- `GET /warmup/report?date=YYYY-MM-DD` for a past day that has a row — `source: "snapshot"`.

---

## What still works without this migration vs what breaks

### Still works (no `warmup_daily_report` table)

- **Today’s Warmup Report** — computed live from `v1.sender_email_performance` (`source: "live"`).
- **Historical dates that still have performance rows** — code falls back to recomputing from `sender_email_performance` when no snapshot row exists (labeled live, not a durable archive).
- **Day picker partially** — `list_available_dates` falls back to distinct `sender_email_performance.snapshot_date` values (and always includes today). Picker still lists dates; rich per-day headline chips from snapshots are missing.
- **Core poller job** — `sender_email_performance` upserts continue; warmup snapshot failure is caught/logged and does not abort the performance write.

### Breaks / degraded until migration is applied

- **Poller upsert into `warmup_daily_report`** — fails (table missing); logs like `Failed to upsert warmup_daily_report` / `Failed to persist warmup_daily_report`. No archive accumulates.
- **True historical snapshots** — dashboard archive / day-over-day deltas that depend on stored bucket + `payload` JSON will not build up; once performance rows age out or differ from the intended end-of-day snapshot, history is incomplete.
- **Day picker archive richness** — without snapshot rows, dates from the performance fallback lack pre-aggregated `total` / `good` / `watch` / etc. until snapshots exist.
- **Serving `source: "snapshot"`** for past dates — unavailable until rows are written after the table exists.

**No other migrations or column changes** are required for the expanded report (`d3175d3`); extra fields live inside `payload` jsonb.

---

## Rollback SQL (if needed)

Only if you must undo this migration. Destroys any snapshot rows written after apply:

```sql
drop index if exists v1.idx_warmup_daily_report_date;
drop table if exists v1.warmup_daily_report;
```

After rollback, behavior returns to the “still works without migration” state above. Does not touch `sender_email_performance` or other tables.

---

## Safety notes

- **Schema is `v1`** — do not create this table in `public`.
- **Idempotent** — `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS`; safe to re-run.
- **No data loss** — additive only; no `ALTER`/`UPDATE`/`DELETE` on existing tables.
- **No RLS / grants in this migration** — matches other recent snapshot tables (e.g. `v1.daily_send_plan` in `015_daily_send_plan.sql`); workers use the **service role** key against schema `v1`.
- **Primary key** `(workspace_id, report_date)` enables daily upserts per workspace without duplicates.
- **Empty table after create is expected** until `poll_sender_email_performance` runs (or you manually call `persist_warmup_daily_report`).

---

## Done when

1. Verification queries show `v1.warmup_daily_report` with the columns/PK/index above.
2. Next successful sender performance poll inserts/updates a row for each workspace for `report_date = today`.
3. Dashboard Warmup Report can pick historical days and receive `source: "snapshot"` for dates that have rows.
