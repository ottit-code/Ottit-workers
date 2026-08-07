"""api/main.py — Ottit CRM Action API (application assembly).

Read endpoints serve from Supabase (kept fresh by pollers); write/action
endpoints proxy to EmailBison/EmailGuard and log to dashboard_action_log.

  Frontend → this API → Supabase (reads) / EmailBison+EmailGuard (writes)

Route handlers live in api/routers/*; shared auth, the counts cache, and small
cross-router helpers live in api/deps.py. This module only wires things
together: CORS, centralized error handling, and router registration.
"""
import logging

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from lib import config

from api.routers.drafter_inbound import router as drafter_inbound_router
from api.routers.drafter_admin import router as drafter_admin_router
from api.routers.inboxassure_spamcheck import router as inboxassure_spamcheck_router
from api.routers.health import router as health_router
from api.routers.stats import router as stats_router
from api.routers.senders import router as senders_router
from api.routers.campaigns import router as campaigns_router
from api.routers.replies import router as replies_router
from api.routers.deliverability import router as deliverability_router
from api.routers.notifications import router as notifications_router
from api.routers.webhooks import router as webhooks_router
from api.routers.aggregates import router as aggregates_router
from api.routers.actions import router as actions_router
from api.routers.schedule import router as schedule_router

logger = logging.getLogger(__name__)

app = FastAPI(title="Ottit CRM API", version="1.0.0")

_cors_origins = config.ALLOWED_ORIGINS if config.ALLOWED_ORIGINS else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=bool(config.ALLOWED_ORIGINS),
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Centralized error handling
# ---------------------------------------------------------------------------
# Route handlers let exceptions propagate rather than echoing str(e) back to
# the client (which leaked DB URLs, driver internals, etc.). These handlers log
# the real error server-side and return a generic, safe message. HTTPException
# is handled natively by FastAPI, so intentional 401/404/503/… are unaffected.

@app.exception_handler(httpx.HTTPError)
async def _handle_upstream_error(request: Request, exc: httpx.HTTPError):
    logger.error("upstream_error %s %s: %r", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=502,
        content={"detail": "Upstream service error. Please retry shortly."},
    )


@app.exception_handler(Exception)
async def _handle_unexpected_error(request: Request, exc: Exception):
    logger.exception("unhandled_error %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error."},
    )


# ---------------------------------------------------------------------------
# Router registration
# ---------------------------------------------------------------------------
for _router in (
    drafter_inbound_router,
    drafter_admin_router,
    inboxassure_spamcheck_router,
    health_router,
    stats_router,
    senders_router,
    campaigns_router,
    replies_router,
    deliverability_router,
    notifications_router,
    webhooks_router,
    aggregates_router,
    actions_router,
    schedule_router,
):
    app.include_router(_router)
