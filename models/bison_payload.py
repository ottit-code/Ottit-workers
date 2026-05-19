"""
Pydantic models for the Bison LEAD_INTERESTED webhook payload.

The shapes mirror https://send.ottit.com/webhook-events/lead-interested.json.
Unknown fields are ignored (`extra='ignore'`) so Bison can evolve the schema
without breaking us.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _Permissive(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class BisonCustomVariable(_Permissive):
    name: str
    value: Optional[str] = None


class BisonLead(_Permissive):
    id: int
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: str
    title: Optional[str] = None
    company: Optional[str] = None
    custom_variables: List[BisonCustomVariable] = Field(default_factory=list)


class BisonReply(_Permissive):
    id: int
    uuid: str
    text_body: str
    html_body: Optional[str] = None
    email_subject: Optional[str] = None
    subject: Optional[str] = None
    raw_message_id: Optional[str] = None
    from_email_address: str
    from_name: Optional[str] = None
    received_at: Optional[datetime] = None
    date_received: Optional[datetime] = None

    @property
    def best_subject(self) -> str:
        return self.email_subject or self.subject or ""


class BisonScheduledEmail(_Permissive):
    raw_message_id: Optional[str] = None
    sequence_step_id: Optional[int] = None
    sequence_step_order: Optional[int] = None
    sequence_step_variant: Optional[int] = None


class BisonSenderEmail(_Permissive):
    id: int
    email: str
    type: Optional[str] = None


class BisonCampaign(_Permissive):
    id: int
    name: Optional[str] = None


class BisonLeadInterestedData(_Permissive):
    reply: BisonReply
    lead: BisonLead
    campaign: BisonCampaign
    scheduled_email: Optional[BisonScheduledEmail] = None
    sender_email: BisonSenderEmail


class BisonEventEnvelope(_Permissive):
    """Outer envelope of any Bison webhook.

    The `event` field is sometimes a dict (`{"type": "..."}`) and sometimes a
    bare string in the wild. We accept both and surface a helper.
    """

    event: Any = None
    data: BisonLeadInterestedData

    @property
    def event_type(self) -> str:
        if isinstance(self.event, dict):
            return str(self.event.get("type") or "")
        if isinstance(self.event, str):
            return self.event
        return ""
