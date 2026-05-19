"""
Load voice assets from Supabase Storage with a TTL cache.

Layer 1 (`instructions.md`) is required — falls back to
`prompts/defaults/instructions.md` if Storage is unreachable.

Layer 2 (the `.skill` bundle) is optional — if absent or unparseable the
loader returns an empty string and the drafter omits it from the system
prompt. The remote path is set by `VOICE_SKILL_PATH` (default:
`saman-cold-email-voice.skill`).

The cache is process-local. With multiple gunicorn workers each one keeps
its own copy; staleness is bounded by VOICE_CACHE_TTL_SECONDS.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

from lib import config, skill_parser

logger = logging.getLogger(__name__)

_DEFAULTS_DIR = Path(__file__).resolve().parent.parent / "prompts" / "defaults"
_FALLBACK_INSTRUCTIONS_PATH = _DEFAULTS_DIR / "instructions.md"


class VoiceLoader:
    """Thread-safe TTL cache over Supabase Storage voice assets."""

    def __init__(
        self,
        supabase_client,
        bucket: str = config.VOICE_BUCKET,
        ttl_seconds: int = config.VOICE_CACHE_TTL_SECONDS,
        instructions_path: str = config.VOICE_INSTRUCTIONS_PATH,
        skill_path: str = config.VOICE_SKILL_PATH,
    ) -> None:
        self._supabase = supabase_client
        self._bucket = bucket
        self._ttl = ttl_seconds
        self._instructions_path = instructions_path
        self._skill_path = skill_path
        self._instructions: str = ""
        self._skill_md: str = ""
        self._expires_at: datetime = datetime.min.replace(tzinfo=timezone.utc)
        self._lock = threading.Lock()

    def get(self) -> Tuple[str, str]:
        """Return (instructions_md, skill_md). Refreshes if cache expired."""
        now = datetime.now(timezone.utc)
        with self._lock:
            if now >= self._expires_at:
                self._refresh()
            return self._instructions, self._skill_md

    def invalidate(self) -> None:
        """Force the next get() to re-fetch from Storage."""
        with self._lock:
            self._expires_at = datetime.min.replace(tzinfo=timezone.utc)

    def _refresh(self) -> None:
        self._instructions = self._load_instructions()
        self._skill_md = self._load_skill_md()
        self._expires_at = datetime.now(timezone.utc) + timedelta(seconds=self._ttl)
        logger.info(
            "voice_loader.refreshed",
            extra={
                "instructions_len": len(self._instructions),
                "skill_md_len": len(self._skill_md),
                "ttl_seconds": self._ttl,
            },
        )

    def _load_instructions(self) -> str:
        blob = self._download(self._instructions_path)
        if blob:
            try:
                return blob.decode("utf-8")
            except UnicodeDecodeError as exc:
                logger.error("voice_loader.instructions_decode_error: %s", exc)
        # Fallback to bundled defaults so the service still drafts something.
        return _read_fallback_instructions()

    def _load_skill_md(self) -> str:
        blob = self._download(self._skill_path)
        if not blob:
            return ""
        return skill_parser.extract_skill_md(blob)

    def _download(self, path: str) -> Optional[bytes]:
        try:
            resp = self._supabase.storage.from_(self._bucket).download(path)
        except Exception as exc:
            logger.warning(
                "voice_loader.download_failed bucket=%s path=%s err=%s",
                self._bucket, path, exc,
            )
            return None
        if isinstance(resp, (bytes, bytearray)):
            return bytes(resp)
        # Some supabase-py versions return a Response-like object.
        data = getattr(resp, "content", None) or getattr(resp, "data", None)
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
        logger.warning("voice_loader.download_unexpected_type type=%s", type(resp).__name__)
        return None


def _read_fallback_instructions() -> str:
    try:
        return _FALLBACK_INSTRUCTIONS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.critical(
            "voice_loader.no_fallback_instructions path=%s — drafts will be unguided",
            _FALLBACK_INSTRUCTIONS_PATH,
        )
        return ""


_singleton: Optional[VoiceLoader] = None
_singleton_lock = threading.Lock()


def get_voice_loader() -> VoiceLoader:
    """Process-singleton accessor used by routers and the drafter orchestrator."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                from lib.supabase_client import get_supabase
                _singleton = VoiceLoader(get_supabase())
    return _singleton
