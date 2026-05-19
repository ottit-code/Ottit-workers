"""Shared FastAPI dependencies for drafter routers (auth)."""
from __future__ import annotations

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from lib import config

_bearer = HTTPBearer(auto_error=False)


def require_drafter_key(creds: HTTPAuthorizationCredentials = Security(_bearer)) -> None:
    """Auth for the inbound webhook from n8n. Skipped if DRAFTER_API_KEY unset."""
    if not config.DRAFTER_API_KEY:
        return
    if creds is None or creds.credentials != config.DRAFTER_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing drafter API key")


def require_admin_key(creds: HTTPAuthorizationCredentials = Security(_bearer)) -> None:
    """Auth for the /admin/* routes. Always required."""
    if not config.ADMIN_API_KEY:
        raise HTTPException(status_code=503, detail="ADMIN_API_KEY not configured")
    if creds is None or creds.credentials != config.ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing admin API key")
