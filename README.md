# ottit-workers

FastAPI service that drafts email replies in Saman's voice (Claude + RAG)
plus a set of pollers that snapshot Bison campaign / sender / lead
engagement data into Supabase.

## Supabase schema: `v1`

All worker-owned tables, RPCs, and views live in the **`v1`** Postgres schema
(the cutover from `public` was completed in the live Supabase project).

This is configured **in exactly one place** —
[`lib/supabase_client.py`](lib/supabase_client.py):

```python
from supabase.client import ClientOptions
_OPTIONS = ClientOptions(schema="v1")
```

Every `.table(...)` and `.rpc(...)` call across the repo (drafter, admin,
9 pollers, dashboard API) automatically resolves against `v1.*` because
PostgREST honours the `Accept-Profile` / `Content-Profile` headers that
`ClientOptions(schema=...)` sets on the client.

If you ever need to read from `public` for a one-off (e.g. legacy data),
override per-call:

```python
get_supabase().schema("public").table("legacy_thing").select("*").execute()
```

**Storage** (the `voice-assets` bucket) is unaffected — Postgres schemas
don't apply to Storage objects.

## Migrations

SQL migrations under [`migrations/`](migrations/) are written to target
the `v1` schema explicitly and are safe to re-run on a fresh project
(everything is `CREATE … IF NOT EXISTS` / `CREATE OR REPLACE`).

## Endpoints (drafter)

- `POST /webhooks/bison/lead-interested` (alias: `/webhooks/n8n/lead-interested`)
  — n8n forwards a Bison `LEAD_INTERESTED` payload; returns Slack-ready draft JSON.
  Accepts the raw Bison envelope, n8n’s `{ body: … }` / `{ json: … }` wrappers,
  or just the inner `data` object.
- `GET  /admin/drafts` — recent drafts (admin bearer).
- `GET  /admin/voice-example-stats` — RAG corpus size + ivfflat
  re-tuning recommendation.
- `POST /admin/backfill-voice-examples` — backfill RAG corpus from
  historical Bison `reply_events`.

## Endpoints (InboxAssure spamcheck)

- `POST /webhooks/inboxassure/spamcheck-completed`
  — n8n forwards InboxAssure `spamcheck.completed`; upserts into
  `v1.inboxassure_spamchecks` + `v1.inboxassure_spamcheck_reports`
  (migration `019_inboxassure_spamchecks.sql`). Accepts the raw event body or
  n8n’s array / `{ body: … }` wrapper.
- Auth is **optional**: missing `Authorization` is allowed; if sent, must be
  `Bearer <DRAFTER_API_KEY>`.
- **Workspace matching** (which Ottit dashboard workspace to show):
  InboxAssure `reports[].workspace_name` (e.g. `"Ottit V2"`) is matched
  case-insensitively to `lib.config.WORKSPACES[].name` → stored as
  `workspace_id` (`ws_v2`). Ensure the InboxAssure workspace is named
  exactly like the Ottit workspace (`Ottit V1` / `Ottit V2`). Optional
  override: `?workspace_id=ws_v2` on the webhook URL.

### n8n forward URL (production)

```
POST https://workers.agent.ottit-bison.app/webhooks/n8n/lead-interested
Authorization: Bearer <DRAFTER_API_KEY>
Content-Type: application/json
```

In n8n: Webhook (Bison) → HTTP Request node with that URL, header
`Authorization: Bearer {{$env.DRAFTER_API_KEY}}`, body JSON =
`{{$json.body}}` (or `{{$json}}` — both work after unwrap). Expect 5–15s.

```
POST https://workers.agent.ottit-bison.app/webhooks/inboxassure/spamcheck-completed
Content-Type: application/json
```

In n8n: Webhook (InboxAssure) → HTTP Request to that URL; body
`{{$json}}` or `{{$json.body}}`. Authorization header optional.

Workspace is resolved from the payload (`workspace_name: "Ottit V2"` →
`ws_v2`). To pin a workspace explicitly (e.g. if IA names diverge):

```
POST …/webhooks/inboxassure/spamcheck-completed?workspace_id=ws_v2
```

## Local dev

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in Supabase + Anthropic + OpenAI keys
uvicorn api.main:app --reload
pytest -q
```

<!-- Redeploy trigger: 2026-08-06 -->
