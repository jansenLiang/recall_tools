from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Session:
    session_id: str
    created_at: float
    mirako_session_id: str
    gateway_url: str
    mode: str
    meeting_provider: str
    meeting_url: str
    bridge_url: str
    recall_bot_id: str
    recall_bot: dict[str, Any]
    created_meeting: dict[str, Any] | None
    should_end_created_meeting: bool
