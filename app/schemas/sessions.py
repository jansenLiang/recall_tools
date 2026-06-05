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
    meeting_provider: Literal["zoom", "google_meet"] = Field(
        default="zoom",
        description="Meeting provider strategy. zoom is implemented; google_meet is reserved for the next strategy implementation.",
    )
    meeting_url: str | None = Field(
        default=None,
        min_length=8,
        description="Existing meeting URL for meeting_provider. If omitted, the zoom strategy creates a Zoom meeting.",
    )
    public_base_url: str | None = Field(
        default=None,
        description="Public HTTPS base URL for this service. Defaults to PUBLIC_BASE_URL.",
    )
    bot_name: str | None = Field(default=None, min_length=1, max_length=100)
    zoom_topic: str | None = None
    zoom_duration_minutes: int | None = Field(default=None, ge=1, le=1440)


class CreateSessionResponse(BaseModel):
    session_id: str
    meeting_url: str
    mirako_session_id: str
    bridge_url: str
    mode: Literal["video", "audio"]
    meeting_provider: Literal["zoom", "google_meet"]
    created_meeting: dict[str, Any] | None = Field(default=None, exclude=True)
    recall_bot_id: str
    recall_bot: dict[str, Any] = Field(default_factory=dict, exclude=True)


class CloseSessionResponse(BaseModel):
    session_id: str
    recall_left: bool
    recall_response: dict[str, Any] | None = None
    gateway_stopped: bool = False
    gateway_response: dict[str, Any] | None = None
    zoom_ended: bool = False
    zoom_response: dict[str, Any] | None = None


class MeetingRecordsResponse(BaseModel):
    records: list[dict[str, Any]]
    pagination: dict[str, Any]


class BridgeTelemetryRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    event: str = Field(min_length=1, max_length=100)
    session_id: str = Field(min_length=1)
    gateway_session_id: str | None = None
    mirako_session_id: str | None = None
    mode: str | None = None
    elapsed_ms: int | None = None
    payload: dict[str, Any] | None = None
