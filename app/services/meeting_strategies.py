from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Protocol

from app.clients.zoom import ZoomMeetingClient
from app.core.config import Settings
from app.schemas.sessions import CreateSessionRequest


logger = logging.getLogger(__name__)


class MeetingStrategy(Protocol):
    async def create_meeting(self, req: CreateSessionRequest) -> dict[str, Any]: ...

    async def end_meeting(self, meeting_id: Any) -> dict[str, Any] | None: ...


class ZoomMeetingStrategy:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def create_meeting(self, req: CreateSessionRequest) -> dict[str, Any]:
        logger.info("zoom strategy create_meeting topic=%s duration_minutes=%s", req.zoom_topic, req.zoom_duration_minutes)
        topic = req.zoom_topic or self.settings.zoom_create_topic or "Mirako Recall Bridge"
        duration = req.zoom_duration_minutes or self.settings.zoom_create_duration_minutes
        return await self._client().create_meeting(
            {
                "topic": topic,
                "type": 2,
                "start_time": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "duration": duration,
                "settings": {
                    "join_before_host": True,
                    "waiting_room": False,
                },
            },
            user_id=self.settings.zoom_create_user_id or "me",
        )

    async def end_meeting(self, meeting_id: Any) -> dict[str, Any] | None:
        return await self._client().end_meeting(meeting_id)

    def _client(self) -> ZoomMeetingClient:
        return ZoomMeetingClient(
            client_id=self.settings.zoom_oauth_client_id,
            client_secret=self.settings.zoom_oauth_client_secret,
            account_id=self.settings.zoom_oauth_account_id,
        )


class GoogleMeetMeetingStrategy:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def create_meeting(self, req: CreateSessionRequest) -> dict[str, Any]:
        logger.error("google_meet strategy create_meeting called but not implemented")
        raise NotImplementedError("google_meet strategy is not implemented yet.")

    async def end_meeting(self, meeting_id: Any) -> dict[str, Any] | None:
        logger.error("google_meet strategy end_meeting called but not implemented meeting_id=%s", meeting_id)
        raise NotImplementedError("google_meet strategy is not implemented yet.")


def get_meeting_strategy(provider: str, settings: Settings) -> MeetingStrategy:
    if provider == "zoom":
        return ZoomMeetingStrategy(settings)
    if provider == "google_meet":
        return GoogleMeetMeetingStrategy(settings)
    raise ValueError(f"Unsupported meeting provider: {provider}")
