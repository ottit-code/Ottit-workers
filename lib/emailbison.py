import httpx
from lib.config import EMAILBISON_API_TOKEN

BASE_URL = "https://send.ottit.com"

_client = httpx.Client(
    base_url=BASE_URL,
    headers={
        "Authorization": f"Bearer {EMAILBISON_API_TOKEN}",
        "Content-Type": "application/json",
    },
    timeout=30,
)


def _extract_list(data) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    raise ValueError(f"Unexpected EmailBison response: {data}")


def get(path: str, params: dict | None = None) -> dict | list:
    res = _client.get(path, params=params)
    res.raise_for_status()
    return res.json()


def patch(path: str, body: dict | None = None) -> dict:
    res = _client.patch(path, json=body or {})
    res.raise_for_status()
    return res.json()


def post(path: str, body: dict | None = None) -> dict:
    res = _client.post(path, json=body or {})
    res.raise_for_status()
    return res.json()


# Convenience wrappers
def get_sender_emails() -> list:
    return _extract_list(get("/api/sender-emails"))

def get_campaigns() -> list:
    return _extract_list(get("/api/campaigns"))

def get_leads(campaign_id: str | None = None) -> list:
    params = {"campaign_id": campaign_id} if campaign_id else None
    return _extract_list(get("/api/leads", params=params))

def get_replies(campaign_id: str | None = None) -> list:
    params = {"campaign_id": campaign_id} if campaign_id else None
    return _extract_list(get("/api/replies", params=params))

def get_campaign_events_stats(start_date: str, end_date: str) -> dict:
    return get("/api/campaign-events/stats", params={"start_date": start_date, "end_date": end_date})

def get_workspace_chart_stats(start_date: str, end_date: str) -> dict:
    return get("/api/workspaces/v1.1/line-area-chart-stats", params={"start_date": start_date, "end_date": end_date})

def get_campaign_sequence_steps(campaign_id: str) -> list:
    return _extract_list(get(f"/api/campaigns/v1.1/{campaign_id}/sequence-steps"))

def get_campaign_scheduled_emails(campaign_id: str) -> list:
    return _extract_list(get(f"/api/campaigns/{campaign_id}/scheduled-emails"))

def get_campaign_line_area_chart_stats(campaign_id: str, start_date: str, end_date: str) -> dict | list:
    return get(f"/api/campaigns/{campaign_id}/line-area-chart-stats",
               params={"start_date": start_date, "end_date": end_date})

def get_campaign_stats(campaign_id: str) -> dict:
    return post(f"/api/campaigns/{campaign_id}/stats")

def get_campaign_details(campaign_id: str) -> dict:
    res = get(f"/api/campaigns/v1.1/{campaign_id}")
    if isinstance(res, dict):
        return res.get("data", res) if "data" in res else res
    return {}

def get_leads_paginated(page: int = 1, per_page: int = 100) -> dict | list:
    """Fetch a page of leads. Returns raw response including meta/pagination info."""
    return get("/api/leads", params={"page": page, "per_page": per_page})

def get_campaign_replies(campaign_id: str, status: str) -> list:
    return _extract_list(get(f"/api/campaigns/{campaign_id}/replies", params={"status": status}))

def get_campaign_email_accounts(campaign_id: str) -> list:
    return _extract_list(get(f"/api/campaigns/{campaign_id}/email-accounts"))
