from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mirako_session_id: str = Field(
        min_length=1,
        description="Mirako interactive session id. Sent to live-stream-gateway as api_key.",
    )
    mode: Literal["video", "audio"] = Field(
        default="video",
        description="video: avatar video + audio into the meeting. audio: audio-only with a black placeholder page.",
    )
    meeting_url: str | None = Field(
        default=None,
        min_length=8,
        description="Existing Zoom/Meet/Teams meeting URL. If omitted, the service creates a Zoom meeting.",
    )
    public_base_url: str | None = Field(
        default=None,
        description="Public HTTPS base URL for this service. Defaults to PUBLIC_BASE_URL.",
    )
    bot_name: str | None = Field(default=None, min_length=1, max_length=100)
    zoom_topic: str | None = None
    zoom_duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    end_created_meeting_on_close: bool = True
    automatic_leave: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None


class CreateSessionResponse(BaseModel):
    session_id: str
    meeting_url: str
    mirako_session_id: str
    bridge_url: str
    mode: Literal["video", "audio"]
    created_meeting: dict[str, Any] | None = None
    recall_bot_id: str
    recall_bot: dict[str, Any]


class CloseSessionResponse(BaseModel):
    session_id: str
    recall_left: bool
    recall_response: dict[str, Any] | None = None
    gateway_stopped: bool = False
    gateway_response: dict[str, Any] | None = None
    zoom_ended: bool = False
    zoom_response: dict[str, Any] | None = None
