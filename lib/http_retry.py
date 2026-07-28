"""Shared tenacity retry policy for outbound HTTP (EmailBison / EmailGuard).

Retries only *transient* failures — connection/timeout errors and 5xx
responses — with exponential backoff + jitter. 4xx responses (auth,
validation, not-found) are never retried. After the final attempt the
original exception is re-raised, so the API layer's httpx.HTTPError handler
still turns it into a clean 502.
"""
from __future__ import annotations

import logging

import httpx
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

logger = logging.getLogger(__name__)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True  # connect errors, read/write timeouts, etc.
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


retry_transient = retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=0.5, max=8),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
