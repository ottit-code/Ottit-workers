from lib.supabase_client import get_supabase
import logging

logger = logging.getLogger(__name__)


def log_action(
    action_type: str,
    entity_type: str = None,
    entity_id: str = None,
    entity_label: str = None,
    payload: dict = None,
    api_response: dict = None,
    status: str = "success",
    error_message: str = None,
    performed_by: str = None,
):
    try:
        get_supabase().table("dashboard_action_log").insert({
            "action_type": action_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "entity_label": entity_label,
            "payload": payload,
            "api_response": api_response,
            "status": status,
            "error_message": error_message,
            "performed_by": performed_by,
        }).execute()
    except Exception as e:
        logger.error(f"Failed to log action {action_type}: {e}")
