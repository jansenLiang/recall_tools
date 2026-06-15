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
    bot_name: str | None = Field(default=None, min_length=1, max_length=100)


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


class ParticipantInfo(BaseModel):
    participant_id: str
    name: str = ""
    is_host: bool = False
    email: str | None = None
    is_screensharing: bool = False
    has_cached_frame: bool = False


class ListParticipantsResponse(BaseModel):
    session_id: str
    mirako_session_id: str
    participants: list[ParticipantInfo]
    last_sharing_participant_id: str | None = None


class CaptureScreenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    participant_id: str | None = Field(
        default=None,
        description="Target participant id from /participants. When omitted, the most recent screenshare frame is used.",
    )
    frame_type: Literal["webcam", "screenshare"] = Field(
        default="screenshare",
        description="Recall video_separate_h264.data stream type to draw from.",
    )
    include_image_base64: bool = Field(
        default=False,
        description="Include the decoded PNG as base64 in the response (debug).",
    )
    prompt: str | None = Field(
        default=None,
        max_length=2000,
        description="Override the MiniMax vision prompt. Falls back to CAPTURE_PROMPT.",
    )


class CaptureScreenResponse(BaseModel):
    session_id: str
    mirako_session_id: str
    participant_id: str
    participant_name: str = ""
    frame_type: Literal["webcam", "screenshare"]
    received_at: float
    captured_at: float
    frame_age_ms: int
    width: int | None = None
    height: int | None = None
    description: str = ""
    image_base64: str | None = None
    mime_type: str = "image/png"
