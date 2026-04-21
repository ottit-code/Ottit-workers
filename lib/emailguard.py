import httpx
from lib.config import EMAILGUARD_API_TOKEN

BASE_URL = "https://app.emailguard.io"

_client = httpx.Client(
    base_url=BASE_URL,
    headers={
        "Authorization": f"Bearer {EMAILGUARD_API_TOKEN}",
        "Content-Type": "application/json",
    },
    timeout=30,
)


def _extract_list(data) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    raise ValueError(f"Unexpected EmailGuard response: {data}")


def get(path: str, params: dict | None = None) -> dict | list:
    res = _client.get(path, params=params)
    res.raise_for_status()
    return res.json()


def post(path: str, body: dict | None = None) -> dict:
    res = _client.post(path, json=body or {})
    res.raise_for_status()
    return res.json()


# Convenience wrappers
def get_inbox_placement_tests() -> list:
    return _extract_list(get("/api/v1/inbox-placement-tests"))

def get_inbox_placement_test(uuid: str) -> dict:
    data = get(f"/api/v1/inbox-placement-tests/{uuid}")
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return data

def get_spam_filter_tests() -> list:
    return _extract_list(get("/api/v1/spam-filter-tests"))

def get_surbl_checks() -> list:
    return _extract_list(get("/api/v1/surbl-blacklist-checks/domains"))

def get_domain_blacklist_checks() -> list:
    """Fetch all pages from /api/v1/blacklist-checks/domains and return combined list."""
    results = []
    page = 1
    while True:
        resp = get("/api/v1/blacklist-checks/domains", params={"page": page})
        results.extend(resp.get("data", []))
        meta = resp.get("meta", {})
        if page >= meta.get("last_page", 1):
            break
        page += 1
    return results
