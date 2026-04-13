# Ottit Workers — Poller Reference

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│  EXISTING WORKERS                                           │
│  stats_poller          every 6h   → sender_daily_stats,    │
│                                     workspace_daily_stats   │
│  delivery_poller       every 2h   → domain_placement_tests, │
│                                     placement_test_emails,  │
│                                     spam_filter_tests,      │
│                                     surbl_checks            │
│  notifier              every 15m  → notifications           │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  NEW WORKERS                                                │
│  ab_test_snapshots_poller    every 6h   → ab_test_snapshots │
│  campaign_daily_stats_poller daily 0 AM → campaign_daily_.. │
│  sender_performance_poller   daily 1 AM → sender_email_perf │
│  lead_engagement_poller      daily 2 AM → lead_engagement_. │
│  reply_events_poller         every 4h  → reply_events       │
└─────────────────────────────────────────────────────────────┘
```

---

## Shared Helper

### `lib/utils.py` — `get_active_campaign_ids(supabase)`

Used by all campaign-scoped pollers. Queries the `documents` table for campaigns with status `active`, `queued`, or `paused`. Falls back to `GET /api/campaigns` if the documents table returns no results.

---

## New Pollers

### 1. `poll_ab_test_snapshots` — every 6 hours

**File:** `workers/ab_test_snapshots_poller.py`  
**Target table:** `ab_test_snapshots`  
**Conflict key:** `(sequence_step_id, snapshot_date)`

**Flow:**
1. Get active campaign IDs via `get_active_campaign_ids()`
2. For each campaign:
   - `GET /api/campaigns/v1.1/{id}/sequence-steps` — step metadata
   - `GET /api/campaigns/{id}/scheduled-emails` — per-email outcomes
3. Aggregate scheduled emails by `sequence_step_id` (sent count, opens, clicks, replies, interested, bounced)
4. Compute rates: `open_rate`, `reply_rate`, `click_rate`, `interest_rate`, `bounce_rate`
5. For variant steps, call `compute_ab_significance` Supabase RPC (skipped gracefully if not deployed)
6. Batch upsert all rows

**API calls per cycle:** `2 × active_campaign_count`

---

### 2. `poll_campaign_daily_stats` — daily at midnight

**File:** `workers/campaign_daily_stats_poller.py`  
**Target table:** `campaign_daily_stats`  
**Conflict key:** `(campaign_id, stat_date)`

**Flow:**
1. Get active campaign IDs
2. For each campaign:
   - `GET /api/campaigns/v1.1/{id}` — name, status, settings
   - `GET /api/campaigns/{id}/line-area-chart-stats?start_date=...&end_date=today`
   - `POST /api/campaigns/{id}/stats` — summary totals (failure is non-fatal)
3. Pivot the time-series response into one row per date
4. Compute `open_rate`, `reply_rate`, `bounce_rate`
5. Batch upsert

**Backfill strategy:**
- **First run** (no rows in `campaign_daily_stats` for this campaign): `start_date = campaign.created_at`
- **Subsequent runs:** `start_date = yesterday` (last 2 days)

**API calls per cycle:** `~3 × active_campaign_count`

---

### 3. `poll_lead_engagement` — daily at 2 AM

**File:** `workers/lead_engagement_poller.py`  
**Target table:** `lead_engagement_snapshots`  
**Conflict key:** `(lead_id, snapshot_date)`

**Flow:**
1. Paginate `GET /api/leads?page={n}&per_page=100` (300ms delay between pages)
2. For each lead:
   - Compute `engagement_score = (replies × 5) + (unique_opens × 2) + (opens × 1)`
   - Determine `funnel_stage` (interested → replied → opened → contacted → uploaded)
   - Map `lead_campaign_data` → `campaign_engagements` JSONB
   - Map `custom_variables` array → `{name: value}` JSONB
3. Batch upsert per page (flush every 100 leads)

**API calls per cycle:** `ceil(total_leads / 100)` (≈ 483 for 48k leads)

**Note:** For very large lead sets, consider filtering by recently-active leads (those with `emails_sent > 0` in the last 7 days) to reduce API load.

---

### 4. `poll_reply_events` — every 4 hours

**File:** `workers/reply_events_poller.py`  
**Target table:** `reply_events`  
**Conflict key:** `(reply_id)` — existing replies are skipped (idempotent)

**Flow:**
1. Get active campaign IDs
2. For each campaign, fetch 3 classification buckets:
   - `GET /api/campaigns/{id}/replies?status=interested`
   - `GET /api/campaigns/{id}/replies?status=not_automated_reply`
   - `GET /api/campaigns/{id}/replies?status=automated_reply`
3. Deduplicate by `reply_id` across all campaigns and classifications
4. Compute `response_time_hours = (replied_at − original_sent_at) / 3600`
5. Batch upsert

**API calls per cycle:** `3 × active_campaign_count`

---

### 5. `poll_sender_email_performance` — daily at 1 AM

**File:** `workers/sender_performance_poller.py`  
**Target table:** `sender_email_performance`  
**Conflict key:** `(sender_email_id, snapshot_date)`

**Flow:**
1. For each active campaign, call `GET /api/campaigns/{id}/email-accounts`
2. Deduplicate senders across campaigns (same sender may appear in multiple campaigns)
3. Batch-fetch cross-reference data from Supabase for all unique sender IDs:
   - `sender_warmup_history` — latest warmup score
   - `sender_recovery` — active (uncompleted) recovery policies
   - `domain_placement_tests` — latest inbox placement score
   - `spam_filter_tests` — latest spam filter score
4. Compute rates: `reply_rate`, `open_rate`, `bounce_rate`, `interest_rate`
5. Call `compute_sender_health_score` Supabase RPC (skipped gracefully if not deployed)
6. Batch upsert

**API calls per cycle:** `active_campaign_count` (one per campaign)  
**Supabase queries:** 4 bulk queries (all senders at once via `IN` filter)

---

## Scheduling

| Poller | Trigger | APScheduler type |
|--------|---------|-----------------|
| `stats_poller` | every 6h | `interval` |
| `delivery_poller` | every 2h | `interval` |
| `notifier` | every 15m | `interval` |
| `ab_test_snapshots_poller` | every 6h | `interval` |
| `reply_events_poller` | every 4h | `interval` |
| `campaign_daily_stats_poller` | daily 0:00 UTC | `cron` |
| `sender_performance_poller` | daily 1:00 UTC | `cron` |
| `lead_engagement_poller` | daily 2:00 UTC | `cron` |

All jobs use `max_instances=1, coalesce=True` to prevent stacking.

On startup, all pollers except `lead_engagement_poller` run immediately.  
`lead_engagement_poller` is intentionally deferred to its 2 AM cron slot to avoid an expensive ~483-request paginated crawl on every deploy.

---

## Running Locally

```bash
# Install dependencies
pip install -r requirements.txt

# Run all pollers (blocking scheduler)
python3 scheduler.py

# Run a single poller manually
python3 -m workers.ab_test_snapshots_poller
python3 -m workers.campaign_daily_stats_poller
python3 -m workers.lead_engagement_poller
python3 -m workers.reply_events_poller
python3 -m workers.sender_performance_poller

# Run tests
python3 -m pytest tests/ -v
```

---

## Environment Variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `SUPABASE_URL` | yes | Supabase project URL |
| `SUPABASE_SERVICE_KEY` | yes | Supabase service role key |
| `EMAILBISON_API_TOKEN` | yes | EmailBison API bearer token |
| `EMAILGUARD_API_TOKEN` | yes | EmailGuard API bearer token |
| `SLACK_WEBHOOK_URL` | no | Slack alerts for critical notifications |
| `API_KEY` | no | Dashboard API auth key (dev mode if unset) |
| `ALLOWED_ORIGINS` | no | Comma-separated CORS origins |
