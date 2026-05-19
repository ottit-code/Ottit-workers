"""
Append-only writes to the `agent_audit` table.

Best-effort: audit failures must never block the drafter pipeline. We log
errors but swallow them. The audit trail is for after-the-fact diagnosis,
not flow control.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from lib.supabase_client import get_supabase

logger = logging.getLogger(__name__)

_TABLE = "agent_audit"
_DEFAULT_RULE = "ai-autoresponder-v1"


def log(
    *,
    action: str,
    target_type: str,
    target_id: str,
    target_email: Optional[str] = None,
    rule: str = _DEFAULT_RULE,
    new_value: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Insert one audit row. Errors are logged but never raised."""
    row = {
        "action": action,
        "target_type": target_type,
        "target_id": str(target_id),
        "target_email": target_email,
        "rule": rule,
        "new_value": new_value or {},
        "metadata": metadata or {},
    }
    try:
        get_supabase().table(_TABLE).insert(row).execute()
    except Exception as exc:
        logger.error("audit.log_failed action=%s target=%s err=%s", action, target_id, exc)
