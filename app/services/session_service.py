from __future__ import annotations

import secrets
import time
from typing import Any

import httpx

from app.clients.recall import RecallClient
from app.clients.zoom import ZoomMeetingClient
from app.core.config import Settings, settings
from app.models.session import Session
from app.schemas.sessions import CloseSessionResponse, CreateSessionRequest, CreateSessionResponse


class SessionServiceError(Exception):
    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


class SessionService:
    def __init__(self, app_settings: Settings) -> None:
        self.settings = app_settings
        self.sessions: dict[str, Session] = {}

    def get_session(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    async def create_session(self, req: CreateSessionRequest) -> CreateSessionResponse:
        mirako_session_id = req.mirako_session_id
        gateway_url = self._normalize_gateway_url(None)

        public_base_url = self._normalize_public_base_url(req.public_base_url)
        session_id = secrets.token_urlsafe(24)
        bridge_url = f"{public_base_url}/bridge/{session_id}"

        created_meeting: dict[str, Any] | None = None
        meeting_url = req.meeting_url
        if not meeting_url:
            created_meeting = await self._create_zoom_meeting(req)
            meeting_url = str(created_meeting.get("join_url") or "")
            if not meeting_url:
                raise SessionServiceError(502, {"error": "zoom_join_url_missing", "zoom": created_meeting})

        self.sessions[session_id] = Session(
            session_id=session_id,
            created_at=time.time(),
            mirako_session_id=mirako_session_id,
            gateway_url=gateway_url,
            mode=req.mode,
            meeting_url=meeting_url,
            bridge_url=bridge_url,
            recall_bot_id="",
            recall_bot={},
            created_meeting=created_meeting,
            should_end_created_meeting=bool(created_meeting and req.end_created_meeting_on_close),
        )

        try:
            bot = await self._create_recall_bot(req, meeting_url=meeting_url, bridge_url=bridge_url)
        except SessionServiceError:
            self.sessions.pop(session_id, None)
            await self._best_effort_end_created_meeting(created_meeting)
            raise

        session = self.sessions[session_id]
        session.recall_bot = bot
        session.recall_bot_id = str(bot.get("id") or bot.get("bot_id") or "")
        if not session.recall_bot_id:
            self.sessions.pop(session_id, None)
            await self._best_effort_end_created_meeting(created_meeting)
            raise SessionServiceError(502, {"error": "recall_bot_id_missing", "bot": bot})

        return CreateSessionResponse(
            session_id=session_id,
            meeting_url=meeting_url,
            mirako_session_id=mirako_session_id,
            bridge_url=bridge_url,
            mode=req.mode,
            created_meeting=created_meeting,
            recall_bot_id=session.recall_bot_id,
            recall_bot=bot,
        )

    async def close_session(self, session_id: str) -> CloseSessionResponse:
        session = self.sessions.pop(session_id, None)
        if session is None:
            raise SessionServiceError(404, "Unknown session_id.")

        recall_response = None
        recall_left = False
        if session.recall_bot_id:
            try:
                recall_response = await self._recall_client().leave_call(session.recall_bot_id)
                recall_left = True
            except Exception as exc:
                raise SessionServiceError(502, self._http_error_detail(exc, service="recall"))

        gateway_response = None
        gateway_stopped = False
        try:
            gateway_response = await self._stop_gateway_session(session)
            gateway_stopped = True
        except Exception as exc:
            raise SessionServiceError(502, self._http_error_detail(exc, service="gateway"))

        zoom_response = None
        zoom_ended = False
        if session.should_end_created_meeting and session.created_meeting:
            meeting_id = session.created_meeting.get("id")
            if meeting_id:
                try:
                    zoom_response = await self._zoom_client().end_meeting(meeting_id)
                    zoom_ended = True
                except Exception as exc:
                    raise SessionServiceError(502, self._http_error_detail(exc, service="zoom"))

        return CloseSessionResponse(
            session_id=session_id,
            recall_left=recall_left,
            recall_response=recall_response,
            gateway_stopped=gateway_stopped,
            gateway_response=gateway_response,
            zoom_ended=zoom_ended,
            zoom_response=zoom_response,
        )

    def _normalize_public_base_url(self, value: str | None) -> str:
        base = (value or self.settings.public_base_url).rstrip("/")
        if base.startswith("http://localhost") or base.startswith("http://127.0.0.1"):
            return base
        if not base.startswith("https://"):
            raise SessionServiceError(
                400,
                "public_base_url must be HTTPS because Recall.ai opens the bridge page from its bot browser.",
            )
        return base

    def _normalize_gateway_url(self, value: str | None) -> str:
        base = (value or self.settings.live_stream_gateway_url).rstrip("/")
        if not base:
            raise SessionServiceError(
                400,
                "gateway_url is required, or set LIVE_STREAM_GATEWAY_URL.",
            )
        if base.startswith("http://localhost") or base.startswith("http://127.0.0.1"):
            return base
        if not base.startswith("https://"):
            raise SessionServiceError(
                400,
                "gateway_url must be HTTPS because Recall.ai opens the bridge page from its bot browser.",
            )
        return base

    async def _create_zoom_meeting(self, req: CreateSessionRequest) -> dict[str, Any]:
        topic = req.zoom_topic or self.settings.zoom_create_topic or "Mirako Recall Bridge"
        duration = req.zoom_duration_minutes or self.settings.zoom_create_duration_minutes
        try:
            return await self._zoom_client().create_meeting(
                {
                    "topic": topic,
                    "type": 1,
                    "duration": duration,
                    "settings": {
                        "join_before_host": True,
                        "waiting_room": False,
                    },
                },
                user_id=self.settings.zoom_create_user_id or "me",
            )
        except Exception as exc:
            raise SessionServiceError(502, self._http_error_detail(exc, service="zoom"))

    async def _create_recall_bot(self, req: CreateSessionRequest, *, meeting_url: str, bridge_url: str) -> dict[str, Any]:
        try:
            return await self._recall_client().create_bot(
                meeting_url=meeting_url,
                bot_name=req.bot_name or self.settings.bot_name,
                variant=self.settings.recall_bot_variant,
                output_media_url=bridge_url,
                automatic_leave=req.automatic_leave,
                metadata=req.metadata,
            )
        except httpx.HTTPStatusError as exc:
            raise SessionServiceError(exc.response.status_code, self._http_error_detail(exc, service="recall"))
        except Exception as exc:
            raise SessionServiceError(502, self._http_error_detail(exc, service="recall"))

    async def _stop_gateway_session(self, session: Session) -> dict[str, Any] | None:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{session.gateway_url}/api/sessions/{session.mirako_session_id}/stop",
                headers={"Accept": "application/json"},
            )
            if response.status_code == 404:
                return {"status_code": 404, "message": "Gateway session was already stopped or not found."}
            response.raise_for_status()
            if not response.content:
                return None
            try:
                return response.json()
            except ValueError:
                return {"status_code": response.status_code, "response": response.text}

    async def _best_effort_leave_recall_bot(self, bot_id: str) -> None:
        try:
            await self._recall_client().leave_call(bot_id)
        except Exception:
            pass

    async def _best_effort_end_created_meeting(self, created_meeting: dict[str, Any] | None) -> None:
        meeting_id = (created_meeting or {}).get("id")
        if not meeting_id:
            return
        try:
            await self._zoom_client().end_meeting(meeting_id)
        except Exception:
            pass

    def _recall_client(self) -> RecallClient:
        return RecallClient(api_key=self.settings.recall_api_key, base_url=self.settings.recall_base_url)

    def _zoom_client(self) -> ZoomMeetingClient:
        return ZoomMeetingClient(
            client_id=self.settings.zoom_oauth_client_id,
            client_secret=self.settings.zoom_oauth_client_secret,
            account_id=self.settings.zoom_oauth_account_id,
        )

    @staticmethod
    def _http_error_detail(exc: Exception, *, service: str) -> dict[str, Any]:
        response = getattr(exc, "response", None)
        detail: dict[str, Any] = {"error": f"{service}_request_failed", "message": str(exc)}
        if response is not None:
            detail["status_code"] = getattr(response, "status_code", None)
            detail["response"] = getattr(response, "text", "")
        return detail


session_service = SessionService(settings)
