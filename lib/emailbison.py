import httpx
from lib.config import EMAILBISON_API_TOKEN

BASE_URL = "https://send.ottit.com"

def _headers() -> dict:
    return {
        "Authorization": f"Bearer {EMAILBISON_API_TOKEN}",
        "Content-Type": "application/json",
    }

def get(path: str, params: dict | None = None) -> dict | list:
    with httpx.Client(timeout=30) as client:
        res = client.get(f"{BASE_URL}{path}", headers=_headers(), params=params)
        res.raise_for_status()
        return res.json()

def patch(path: str, body: dict | None = None) -> dict:
    with httpx.Client(timeout=30) as client:
        res = client.patch(f"{BASE_URL}{path}", headers=_headers(), json=body or {})
        res.raise_for_status()
        return res.json()

def post(path: str, body: dict | None = None) -> dict:
    with httpx.Client(timeout=30) as client:
        res = client.post(f"{BASE_URL}{path}", headers=_headers(), json=body or {})
        res.raise_for_status()
        return res.json()

# Convenience wrappers
def get_sender_emails() -> list:
    data = get("/api/sender-emails")
    if isinstance(data, list):
        return data
    return data.get("data", [])

def get_campaigns() -> list:
    data = get("/api/campaigns")
    if isinstance(data, list):
        return data
    return data.get("data", [])

def get_campaign_events_stats(start_date: str, end_date: str) -> dict:
    return get("/api/campaign-events/stats", params={"start_date": start_date, "end_date": end_date})
