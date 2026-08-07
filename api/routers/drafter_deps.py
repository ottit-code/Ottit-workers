"""Shared FastAPI dependencies for drafter routers (auth)."""
from __future__ import annotations

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from lib import config

_bearer = HTTPBearer(auto_error=False)


def require_drafter_key(creds: HTTPAuthorizationCredentials = Security(_bearer)) -> None:
    """Optional auth for inbound webhooks (n8n / Bison forwards).

    Missing Authorization is allowed so n8n can POST without a header.
    If Authorization is sent, it must be Bearer DRAFTER_API_KEY.

    When DRAFTER_API_KEY is unset: open in development; 503 otherwise
    (so prod cannot silently go open while still accepting forged Bearer tokens).
    """
    if not config.DRAFTER_API_KEY:
        if config.APP_ENV == "development":
            return
        raise HTTPException(status_code=503, detail="DRAFTER_API_KEY not configured")
    if creds is None:
        return
    if creds.credentials != config.DRAFTER_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid drafter API key")


def require_admin_key(creds: HTTPAuthorizationCredentials = Security(_bearer)) -> None:
    """Auth for the /admin/* routes. Always required."""
    if not config.ADMIN_API_KEY:
        raise HTTPException(status_code=503, detail="ADMIN_API_KEY not configured")
    if creds is None or creds.credentials != config.ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing admin API key")
