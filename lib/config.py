import os
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
EMAILBISON_API_TOKEN = os.environ["EMAILBISON_API_TOKEN"]
EMAILGUARD_API_TOKEN = os.environ["EMAILGUARD_API_TOKEN"]
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "")

# API auth — if unset, auth is skipped (dev mode)
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
CLAUDE_MODEL_PRIMARY       = os.getenv("CLAUDE_MODEL_PRIMARY", "claude-opus-4-7")
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
