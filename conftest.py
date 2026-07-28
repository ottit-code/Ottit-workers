"""
Root conftest.py — sets dummy environment variables so that lib.config
and lib.emailbison can be imported in tests without a real .env file.
These values are overridden only when not already present in the environment.
"""
import os

# Tests run as a development environment: auth dependencies keep their
# unset-key bypass (fail-closed only kicks in outside development).
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("SUPABASE_URL", "http://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-service-key")
os.environ.setdefault("EMAILBISON_API_TOKEN", "test-emailbison-token")
os.environ.setdefault("EMAILGUARD_API_TOKEN", "test-emailguard-token")
os.environ.setdefault("DRAFTER_API_KEY", "test-drafter-key")
os.environ.setdefault("ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
