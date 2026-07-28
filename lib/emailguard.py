from datetime import datetime, timedelta, timezone

import httpx
from lib.config import DEFAULT_WORKSPACE_ID, EMAILGUARD_API_TOKEN, get_workspace
from lib.http_retry import retry_transient

BASE_URL = "https://app.emailguard.io"


def _cutoff_iso(days: int) -> str:
    """UTC date `days` ago (YYYY-MM-DD). Date-only so it string-compares
    correctly against both EmailGuard created_at shapes
    ('2026-07-27T01:09:56.000000Z' and '2026-07-27'), keeping the boundary day."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


class GuardClient:
    """EmailGuard API client bound to one workspace token."""

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
        raise ValueError(f"Unexpected EmailGuard response: {data}")

    @retry_transient
    def get(self, path: str, params: dict | None = None) -> dict | list:
        res = self._client.get(path, params=params)
        res.raise_for_status()
        return res.json()

    @retry_transient
    def post(self, path: str, body: dict | None = None) -> dict:
        res = self._client.post(path, json=body or {})
        res.raise_for_status()
        return res.json()

    def get_all(
        self,
        path: str,
        params: dict | None = None,
        max_pages: int = 300,
        newer_than: str | None = None,
    ) -> list:
        """Every page of a paginated EmailGuard list endpoint, combined.

        EmailGuard lists are newest-first; passing `newer_than` (ISO date/time
        string) stops paging once items older than the cutoff appear, so huge
        histories (36k placement tests, 141k SURBL checks) aren't re-walked on
        every poll. Items without created_at are kept.
        """
        items: list = []
        page = 1
        while True:
            res = self.get(path, params={**(params or {}), "page": page})
            if isinstance(res, list):
                items.extend(res)
                return items
            data = res.get("data") or []
            if newer_than:
                fresh = [
                    d for d in data
                    if not d.get("created_at") or d["created_at"] >= newer_than
                ]
                items.extend(fresh)
                if len(fresh) < len(data):
                    return items  # crossed the cutoff — older pages only from here
            else:
                items.extend(data)
            meta = res.get("meta") or {}
            if page >= int(meta.get("last_page") or 1) or page >= max_pages:
                return items
            page += 1

    # Convenience wrappers
    def get_inbox_placement_tests(self, days: int = 3) -> list:
        """Placement tests created in the last `days` days (poller overlap
        window). Pass days=0 for the first page only."""
        if days <= 0:
            return self._extract_list(self.get("/api/v1/inbox-placement-tests"))
        return self.get_all(
            "/api/v1/inbox-placement-tests", newer_than=_cutoff_iso(days)
        )

    def get_inbox_placement_test(self, uuid: str) -> dict:
        data = self.get(f"/api/v1/inbox-placement-tests/{uuid}")
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data

    def get_spam_filter_tests(self) -> list:
        return self.get_all("/api/v1/spam-filter-tests")

    def get_surbl_checks(self, days: int = 30) -> list:
        """SURBL checks from the last `days` days (the full history is 140k+
        rows; older results never change)."""
        return self.get_all(
            "/api/v1/surbl-blacklist-checks/domains", newer_than=_cutoff_iso(days)
        )

    def get_domain_blacklist_checks(self) -> list:
        """Fetch all pages from /api/v1/blacklist-checks/domains and return combined list."""
        results = []
        page = 1
        while True:
            resp = self.get("/api/v1/blacklist-checks/domains", params={"page": page})
            results.extend(resp.get("data", []))
            meta = resp.get("meta", {})
            if page >= meta.get("last_page", 1):
                break
            page += 1
        return results


# ---------------------------------------------------------------------------
# Workspace-aware client registry
# ---------------------------------------------------------------------------

_default_client = GuardClient(EMAILGUARD_API_TOKEN)
_clients: dict[str, GuardClient] = {DEFAULT_WORKSPACE_ID: _default_client}


def for_workspace(workspace_id: str) -> GuardClient:
    """Client for a configured workspace. Raises if the workspace is
    registered but has no EmailGuard token — never falls back to the default
    token, which would mislabel V1 data as this workspace."""
    client = _clients.get(workspace_id)
    if client is None:
        ws = get_workspace(workspace_id)
        if ws and not ws.get("eg_token"):
            raise RuntimeError(
                f"Workspace {workspace_id} has no EmailGuard token configured"
            )
        client = GuardClient(ws["eg_token"]) if ws else _default_client
        _clients[workspace_id] = client
    return client


# ---------------------------------------------------------------------------
# Module-level API — bound to the default (ws_v1) workspace.
# Kept so existing single-workspace call sites work unchanged.
# ---------------------------------------------------------------------------

def _extract_list(data) -> list:
    return GuardClient._extract_list(data)


def get(path: str, params: dict | None = None) -> dict | list:
    return _default_client.get(path, params=params)


def post(path: str, body: dict | None = None) -> dict:
    return _default_client.post(path, body)


def get_inbox_placement_tests(days: int = 3) -> list:
    return _default_client.get_inbox_placement_tests(days)

def get_inbox_placement_test(uuid: str) -> dict:
    return _default_client.get_inbox_placement_test(uuid)

def get_spam_filter_tests() -> list:
    return _default_client.get_spam_filter_tests()

def get_surbl_checks(days: int = 30) -> list:
    return _default_client.get_surbl_checks(days)

def get_domain_blacklist_checks() -> list:
    return _default_client.get_domain_blacklist_checks()
