import httpx
from lib.config import EMAILGUARD_API_TOKEN

BASE_URL = "https://app.emailguard.io"

def _headers() -> dict:
    return {
        "Authorization": f"Bearer {EMAILGUARD_API_TOKEN}",
        "Content-Type": "application/json",
    }

def get(path: str, params: dict | None = None) -> dict | list:
    with httpx.Client(timeout=30) as client:
        res = client.get(f"{BASE_URL}{path}", headers=_headers(), params=params)
        res.raise_for_status()
        return res.json()

def post(path: str, body: dict | None = None) -> dict:
    with httpx.Client(timeout=30) as client:
        res = client.post(f"{BASE_URL}{path}", headers=_headers(), json=body or {})
        res.raise_for_status()
        return res.json()

# Convenience wrappers
def get_inbox_placement_tests() -> list:
    data = get("/api/v1/inbox-placement-tests")
    if isinstance(data, list):
        return data
    return data.get("data", [])

def get_inbox_placement_test(uuid: str) -> dict:
    data = get(f"/api/v1/inbox-placement-tests/{uuid}")
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return data

def get_spam_filter_tests() -> list:
    data = get("/api/v1/spam-filter-tests")
    if isinstance(data, list):
        return data
    return data.get("data", [])

def get_surbl_checks() -> list:
    data = get("/api/v1/surbl-blacklist-checks/domains")
    if isinstance(data, list):
        return data
    return data.get("data", [])
