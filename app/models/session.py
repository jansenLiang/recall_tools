from __future__ import annotations

from dataclasses import dataclass, field
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
    transcript_utterances: list[dict[str, Any]]
    meeting_participants: dict[str, dict[str, Any]]
    meeting_participant_count: int
    last_non_bot_participant_left_at: float | None
    has_seen_non_bot_participant: bool
    bot_only_cleanup_started: bool
    conversation_mode: str
    created_meeting: dict[str, Any] | None
    should_end_created_meeting: bool
    gateway_stopped: bool = False
    closed: bool = False
    closed_reason: str | None = None
    closed_at: float | None = None
    sharing_participant_ids: set[str] = field(default_factory=set)
    last_sharing_participant_id: str | None = None
    last_sharing_event_at: float | None = None
