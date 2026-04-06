"""
lib/notifications.py — shared notification creation utility

Used by both api/main.py (webhook-triggered) and workers/notifier.py (poll-triggered).
"""

import logging
import json
import urllib.request
from lib.supabase_client import get_supabase
from lib import config

logger = logging.getLogger(__name__)


def create_notification(
    severity: str,
    type_: str,
    title: str,
    body: str,
    entity_type: str = None,
    entity_id: str = None,
) -> None:
    """Write a notification row to Supabase and fire Slack for critical severity."""
    supabase = get_supabase()
    row = {
        "severity": severity,
        "type": type_,
        "title": title,
        "body": body,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "read": False,
    }
    try:
        supabase.table("notifications").insert(row).execute()
        logger.info(f"Created notification: [{severity}] {title}")
        if severity == "critical" and config.SLACK_WEBHOOK_URL:
            _send_slack(title, body, severity)
    except Exception as e:
        logger.error(f"Failed to create notification: {e}")


def _send_slack(title: str, body: str, severity: str) -> None:
    emoji = {"critical": "🔴", "warning": "🟠", "info": "🟡", "resolved": "✅"}.get(severity, "ℹ️")
    payload = json.dumps({"text": f"{emoji} *{title}*\n{body}"}).encode("utf-8")
    req = urllib.request.Request(
        config.SLACK_WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        logger.error(f"Slack notification failed: {e}")
