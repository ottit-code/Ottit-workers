import json

import httpx
from lib.config import DEFAULT_WORKSPACE_ID, EMAILBISON_API_TOKEN, get_workspace
from lib.http_retry import retry_transient

BASE_URL = "https://send.ottit.com"


class BisonClient:
    """EmailBison API client bound to one workspace token."""

    def __init__(self, token: str):
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )

    @staticmethod
    def _extract_list(data) -> list:
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        raise ValueError(f"Unexpected EmailBison response: {data}")

    @retry_transient
    def get(self, path: str, params: dict | None = None) -> dict | list:
        res = self._client.get(path, params=params)
        res.raise_for_status()
        return res.json()

    @retry_transient
    def get_with_body(self, path: str, body: dict) -> dict | list:
        """GET with a JSON body — several Bison endpoints require this
        (e.g. /api/campaigns/sending-schedules). httpx.Client.get() rejects
        json=, so we go through request()."""
        res = self._client.request(
            "GET",
            path,
            content=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        res.raise_for_status()
        return res.json()

    def get_all(self, path: str, params: dict | None = None, max_pages: int = 1000) -> list:
        """Every page of a Laravel-paginated list endpoint, combined.

        Bison ignores per_page (always 15/page) and lists oldest-first, so a
        single-page read silently returns only the 15 *oldest* records.
        """
        items: list = []
        page = 1
        while True:
            res = self.get(path, params={**(params or {}), "page": page})
            if isinstance(res, list):
                # Endpoint isn't paginated after all.
                items.extend(res)
                return items
            items.extend(res.get("data") or [])
            meta = res.get("meta") or {}
            last_page = int(meta.get("last_page") or 1)
            if page >= last_page or page >= max_pages:
                return items
            page += 1

    @retry_transient
    def patch(self, path: str, body: dict | None = None) -> dict:
        res = self._client.patch(path, json=body or {})
        res.raise_for_status()
        return res.json()

    @retry_transient
    def post(self, path: str, body: dict | None = None) -> dict:
        res = self._client.post(path, json=body or {})
        res.raise_for_status()
        return res.json()

    # Convenience wrappers — all list endpoints walk every page.
    def get_sender_emails(self) -> list:
        return self.get_all("/api/sender-emails")

    def get_campaigns(self) -> list:
        return self.get_all("/api/campaigns")

    def get_leads(self, campaign_id: str | None = None, max_pages: int = 14) -> list:
        """Live leads listing. Capped by default — the workspace holds 250k+
        leads at a fixed 15/page, so 'all pages' is not viable for API views.
        Full lead data comes from lead_engagement_poller via Supabase."""
        params = {"campaign_id": campaign_id} if campaign_id else None
        return self.get_all("/api/leads", params=params, max_pages=max_pages)

    def get_replies(self, campaign_id: str | None = None, max_pages: int = 14) -> list:
        """Live replies listing. Capped by default (100k+ replies at 15/page);
        durable reply data comes from reply_events_poller via Supabase."""
        params = {"campaign_id": campaign_id} if campaign_id else None
        return self.get_all("/api/replies", params=params, max_pages=max_pages)

    def get_campaign_events_stats(
        self,
        start_date: str,
        end_date: str,
        campaign_ids: list[int | str] | None = None,
    ) -> dict | list:
        """Workspace (or campaign-filtered) event series — Sent/Opened/etc."""
        params: dict = {"start_date": start_date, "end_date": end_date}
        if campaign_ids:
            params["campaign_ids"] = [int(c) for c in campaign_ids]
        return self.get("/api/campaign-events/stats", params=params)

    def get_workspace_chart_stats(self, start_date: str, end_date: str) -> dict:
        return self.get("/api/workspaces/v1.1/line-area-chart-stats", params={"start_date": start_date, "end_date": end_date})

    def get_campaign_sequence_steps(self, campaign_id: str) -> list:
        # Response: {"data": {"sequence_id": int, "sequence_steps": [...]}}
        data = self.get(f"/api/campaigns/v1.1/{campaign_id}/sequence-steps")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            inner = data.get("data", data)
            if isinstance(inner, list):
                return inner
            if isinstance(inner, dict):
                return inner.get("sequence_steps", [])
        return []

    def get_campaign_scheduled_emails(self, campaign_id: str) -> list:
        return self.get_all(f"/api/campaigns/{campaign_id}/scheduled-emails")

    def get_sending_schedules(self, day: str) -> list:
        """Per-campaign emails_being_sent for today|tomorrow|day_after_tomorrow.

        One call replaces paging /api/campaigns/{id}/scheduled-emails (15/page).
        """
        if day not in {"today", "tomorrow", "day_after_tomorrow"}:
            raise ValueError(f"invalid sending-schedules day: {day}")
        data = self.get_with_body("/api/campaigns/sending-schedules", {"day": day})
        return self._extract_list(data)

    def get_campaign_line_area_chart_stats(self, campaign_id: str, start_date: str, end_date: str) -> dict | list:
        return self.get(f"/api/campaigns/{campaign_id}/line-area-chart-stats",
                        params={"start_date": start_date, "end_date": end_date})

    def get_campaign_stats(
        self,
        campaign_id: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict:
        body: dict = {}
        if start_date and end_date:
            body = {"start_date": start_date, "end_date": end_date}
        return self.post(f"/api/campaigns/{campaign_id}/stats", body or None)

    def get_campaign_details(self, campaign_id: str) -> dict:
        res = self.get(f"/api/campaigns/{campaign_id}")
        if isinstance(res, dict):
            return res.get("data", res) if "data" in res else res
        return {}

    def get_leads_paginated(self, page: int = 1, per_page: int = 100) -> dict | list:
        """Fetch a page of leads. Returns raw response including meta/pagination info."""
        return self.get("/api/leads", params={"page": page, "per_page": per_page})

    def get_campaign_replies(self, campaign_id: str, status: str) -> list:
        return self.get_all(f"/api/campaigns/{campaign_id}/replies", params={"status": status})

    def get_campaign_email_accounts(self, campaign_id: str) -> list:
        return self.get_all(f"/api/campaigns/{campaign_id}/sender-emails")

    def get_warmup_sender_emails(self) -> list:
        """Live warmup stats per sender (score, daily limit, sent/replies/spam
        saves). This is the only Bison source of warmup_score."""
        return self.get_all("/api/warmup/sender-emails")


# ---------------------------------------------------------------------------
# Workspace-aware client registry
# ---------------------------------------------------------------------------

_default_client = BisonClient(EMAILBISON_API_TOKEN)
_clients: dict[str, BisonClient] = {DEFAULT_WORKSPACE_ID: _default_client}


def for_workspace(workspace_id: str) -> BisonClient:
    """Client for a configured workspace. Falls back to the default token
    when the workspace is unknown (defensive; callers pass registry ids)."""
    client = _clients.get(workspace_id)
    if client is None:
        ws = get_workspace(workspace_id)
        if ws and not ws.get("bison_token"):
            # Registered workspace without a token (e.g. ws_v2 before
            # EMAILBISON_API_TOKEN_V2 is set). Never fall back to the default
            # token — that would mislabel V1 data as this workspace.
            raise RuntimeError(
                f"Workspace {workspace_id} has no EmailBison token configured"
            )
        client = BisonClient(ws["bison_token"]) if ws else _default_client
        _clients[workspace_id] = client
    return client


# ---------------------------------------------------------------------------
# Module-level API — bound to the default (ws_v1) workspace.
# Kept so existing single-workspace call sites work unchanged.
# ---------------------------------------------------------------------------

def _extract_list(data) -> list:
    return BisonClient._extract_list(data)


def get(path: str, params: dict | None = None) -> dict | list:
    return _default_client.get(path, params=params)


def patch(path: str, body: dict | None = None) -> dict:
    return _default_client.patch(path, body)


def post(path: str, body: dict | None = None) -> dict:
    return _default_client.post(path, body)


def get_sender_emails() -> list:
    return _default_client.get_sender_emails()

def get_campaigns() -> list:
    return _default_client.get_campaigns()

def get_leads(campaign_id: str | None = None) -> list:
    return _default_client.get_leads(campaign_id)

def get_replies(campaign_id: str | None = None) -> list:
    return _default_client.get_replies(campaign_id)

def get_campaign_events_stats(start_date: str, end_date: str) -> dict:
    return _default_client.get_campaign_events_stats(start_date, end_date)

def get_workspace_chart_stats(start_date: str, end_date: str) -> dict:
    return _default_client.get_workspace_chart_stats(start_date, end_date)

def get_campaign_sequence_steps(campaign_id: str) -> list:
    return _default_client.get_campaign_sequence_steps(campaign_id)

def get_campaign_scheduled_emails(campaign_id: str) -> list:
    return _default_client.get_campaign_scheduled_emails(campaign_id)

def get_sending_schedules(day: str) -> list:
    return _default_client.get_sending_schedules(day)

def get_campaign_line_area_chart_stats(campaign_id: str, start_date: str, end_date: str) -> dict | list:
    return _default_client.get_campaign_line_area_chart_stats(campaign_id, start_date, end_date)

def get_campaign_stats(
    campaign_id: str, start_date: str | None = None, end_date: str | None = None
) -> dict:
    return _default_client.get_campaign_stats(campaign_id, start_date, end_date)

def get_campaign_details(campaign_id: str) -> dict:
    return _default_client.get_campaign_details(campaign_id)

def get_leads_paginated(page: int = 1, per_page: int = 100) -> dict | list:
    return _default_client.get_leads_paginated(page, per_page)

def get_campaign_replies(campaign_id: str, status: str) -> list:
    return _default_client.get_campaign_replies(campaign_id, status)

def get_campaign_email_accounts(campaign_id: str) -> list:
    return _default_client.get_campaign_email_accounts(campaign_id)
