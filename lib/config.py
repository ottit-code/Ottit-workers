import os
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------
# Each workspace maps to its own EmailBison + EmailGuard credentials
# (suffixed env vars: *_V1, *_V2; legacy unsuffixed names are V1 fallbacks).
# Pollers loop over the pollable (token-configured) workspaces and stamp
# workspace_id on every snapshot row; read endpoints accept an optional
# workspace_id filter ("all"/omitted = aggregate). Both workspaces are
# ALWAYS registered so the dashboard switcher lists V1 + V2 even before a
# token is configured — a workspace without a token simply has no fresh data.

EMAILBISON_API_TOKEN = (
    os.getenv("EMAILBISON_API_TOKEN_V1") or os.environ["EMAILBISON_API_TOKEN"]
)
EMAILBISON_API_TOKEN_V2 = os.getenv("EMAILBISON_API_TOKEN_V2", "")
EMAILGUARD_API_TOKEN = (
    os.getenv("EMAILGUARD_API_TOKEN_V1") or os.environ["EMAILGUARD_API_TOKEN"]
)
EMAILGUARD_API_TOKEN_V2 = os.getenv("EMAILGUARD_API_TOKEN_V2", "")

DEFAULT_WORKSPACE_ID = "ws_v1"

WORKSPACES: list[dict] = [
    {
        "id": "ws_v1",
        "name": "Ottit V1",
        "bison_token": EMAILBISON_API_TOKEN,
        "bison_team_id": os.getenv("EMAILBISON_TEAM_ID_V1") or os.getenv("EMAILBISON_TEAM_ID", ""),
        "eg_token": EMAILGUARD_API_TOKEN,
        "eg_workspace_uuid": os.getenv("EMAILGUARD_WORKSPACE_UUID_V1")
        or os.getenv("EMAILGUARD_WORKSPACE_UUID", ""),
    },
    {
        "id": "ws_v2",
        "name": "Ottit V2",
        "bison_token": EMAILBISON_API_TOKEN_V2,
        "bison_team_id": os.getenv("EMAILBISON_TEAM_ID_V2", ""),
        "eg_token": EMAILGUARD_API_TOKEN_V2,
        "eg_workspace_uuid": os.getenv("EMAILGUARD_WORKSPACE_UUID_V2", ""),
    },
]


def pollable_workspaces() -> list[dict]:
    """Workspaces with an EmailBison token — the only ones pollers can fetch."""
    return [ws for ws in WORKSPACES if ws.get("bison_token")]


def eg_pollable_workspaces() -> list[dict]:
    """Workspaces with an EmailGuard token — for deliverability pollers."""
    return [ws for ws in WORKSPACES if ws.get("eg_token")]


def get_workspace(workspace_id: str) -> dict | None:
    for ws in WORKSPACES:
        if ws["id"] == workspace_id:
            return ws
    return None

# ---------------------------------------------------------------------------
# InboxAssure (placement scores) — integration is dormant until the API
# token is provided. The poller and endpoints no-op when unset.
# ---------------------------------------------------------------------------
INBOXASSURE_API_TOKEN = os.getenv("INBOXASSURE_API_TOKEN", "")
INBOXASSURE_BASE_URL = os.getenv("INBOXASSURE_BASE_URL", "https://inboxassure.app")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "")

# Deployment environment. An auth bypass on a missing key is only honoured
# when this is "development"; anywhere else a missing key fails closed (503)
# so a misconfigured prod deploy can't silently serve an unauthenticated API.
APP_ENV = os.getenv("APP_ENV", "production").strip().lower()

# API auth — if unset, auth is skipped ONLY in development (see APP_ENV).
API_KEY = os.getenv("API_KEY", "")
# Comma-separated allowed origins for CORS, e.g. "https://app.ottit.com,http://localhost:3000"
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]

try:
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
except ValueError:
    SMTP_PORT = 587

# ---------------------------------------------------------------------------
# Drafter (AI auto-responder)
# ---------------------------------------------------------------------------

DRAFTER_API_KEY            = os.getenv("DRAFTER_API_KEY", "")
ADMIN_API_KEY              = os.getenv("ADMIN_API_KEY", "")
ANTHROPIC_API_KEY          = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY             = os.getenv("OPENAI_API_KEY", "")
CLAUDE_MODEL_PRIMARY       = os.getenv("CLAUDE_MODEL_PRIMARY", "claude-opus-5")
CLAUDE_MODEL_ENSEMBLE      = os.getenv("CLAUDE_MODEL_ENSEMBLE", "claude-haiku-4-5")
OPENAI_EMBEDDING_MODEL     = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
VOICE_BUCKET               = os.getenv("VOICE_BUCKET", "voice-assets")
VOICE_INSTRUCTIONS_PATH    = os.getenv("VOICE_INSTRUCTIONS_PATH", "saman-writing-style-cold-email.md")
VOICE_SKILL_PATH           = os.getenv("VOICE_SKILL_PATH", "saman-cold-email-voice.skill")

try:
    VOICE_CACHE_TTL_SECONDS = int(os.getenv("VOICE_CACHE_TTL_SECONDS", "300"))
except ValueError:
    VOICE_CACHE_TTL_SECONDS = 300

try:
    RAG_TOP_K = int(os.getenv("RAG_TOP_K", "5"))
except ValueError:
    RAG_TOP_K = 5

try:
    DRAFT_TIMEOUT_SECONDS = int(os.getenv("DRAFT_TIMEOUT_SECONDS", "60"))
except ValueError:
    DRAFT_TIMEOUT_SECONDS = 60
