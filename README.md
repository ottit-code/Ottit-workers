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

- `POST /webhooks/bison/lead-interested` — n8n calls this with a Bison
  `LEAD_INTERESTED` payload, gets back a Slack-ready draft JSON.
- `GET  /admin/drafts` — recent drafts (admin bearer).
- `GET  /admin/voice-example-stats` — RAG corpus size + ivfflat
  re-tuning recommendation.
- `POST /admin/backfill-voice-examples` — backfill RAG corpus from
  historical Bison `reply_events`.

## Local dev

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in Supabase + Anthropic + OpenAI keys
uvicorn api.main:app --reload
pytest -q
```

<!-- Redeploy trigger: 2026-08-06 -->
