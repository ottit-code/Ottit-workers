"""InboxAssure API client — READ-ONLY placement results fetch.

InboxAssure (inboxassure.app) has no public API documentation yet; the
endpoint paths below are provisional and centralised here so that only this
module needs adjusting once the real API docs + key arrive.

Configuration (env):
  INBOXASSURE_API_TOKEN — bearer token; integration is dormant until set.
  INBOXASSURE_BASE_URL  — defaults to https://inboxassure.app

This client never launches tests — it only fetches existing results.
"""
import httpx
from lib.config import INBOXASSURE_API_TOKEN, INBOXASSURE_BASE_URL
from lib.http_retry import retry_transient

# TODO(inboxassure): confirm the results path once API docs are available.
RESULTS_PATH = "/api/v1/placement-tests"

_client: httpx.Client | None = None


def is_configured() -> bool:
    return bool(INBOXASSURE_API_TOKEN)


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        if not is_configured():
            raise RuntimeError("INBOXASSURE_API_TOKEN is not configured")
        _client = httpx.Client(
            base_url=INBOXASSURE_BASE_URL,
            headers={
                "Authorization": f"Bearer {INBOXASSURE_API_TOKEN}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
    return _client


def _extract_list(data) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "data" in data:
        inner = data["data"]
        if isinstance(inner, list):
            return inner
    raise ValueError(f"Unexpected InboxAssure response: {data}")


@retry_transient
def get(path: str, params: dict | None = None) -> dict | list:
    res = _get_client().get(path, params=params)
    res.raise_for_status()
    return res.json()


def get_placement_results() -> list:
    """All available placement test results (latest state, read-only)."""
    return _extract_list(get(RESULTS_PATH))
