from __future__ import annotations

import base64
import io
import logging
import secrets
import time
from typing import Any

import av
import httpx

from app.clients.recall import RecallClient
from app.core.config import Settings, settings
from app.models.session import Session
from app.schemas.sessions import (
    CaptureScreenRequest,
    CaptureScreenResponse,
    CloseSessionResponse,
    CreateSessionRequest,
    CreateSessionResponse,
    ListParticipantsResponse,
    ParticipantInfo,
)
from app.services.frame_cache import CachedFrame, H264FrameCache
from app.services.hermes_agent import HermesAgentClient, HermesAgentError
from app.services.meeting_strategies import get_meeting_strategy
from app.services.minimax_vision import (
    MinimaxVisionClient,
    MinimaxVisionError,
    normalize_mime,
)
from app.services.recall_realtime import H264_EVENT, RecallRealtimeClient
from app.services.recall_store import recall_store


logger = logging.getLogger(__name__)


class SessionServiceError(Exception):
    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


class SessionService:
    def __init__(self, app_settings: Settings) -> None:
        self.settings = app_settings
        self.sessions: dict[str, Session] = {}
        self._frame_caches: dict[str, H264FrameCache] = {}
        self._realtime_clients: dict[str, RecallRealtimeClient] = {}

    def get_session(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    def get_frame_cache(self, session_id: str) -> H264FrameCache | None:
        return self._frame_caches.get(session_id)

    def get_session_by_recall_bot_id(self, bot_id: str) -> Session | None:
        for session in self.sessions.values():
            if session.recall_bot_id == bot_id:
                return session
        return None

    def get_bridge_status(self, session_id: str) -> dict[str, Any] | None:
        session = self.get_session(session_id)
        if session is None:
            return None
        return {
            "session_id": session.session_id,
            "mirako_session_id": session.mirako_session_id,
            "closed": session.closed,
            "closed_reason": session.closed_reason,
            "closed_at": session.closed_at,
            "gateway_stopped": session.gateway_stopped,
            "can_connect_gateway": not session.closed,
        }

    async def create_session(self, req: CreateSessionRequest) -> CreateSessionResponse:
        mirako_session_id = req.mirako_session_id
        gateway_url = self._normalize_gateway_url(None)
        existing_for_mirako = [
            session
            for session in self.sessions.values()
            if session.mirako_session_id == mirako_session_id
        ]
        if existing_for_mirako:
            logger.info(
                "create_session sees existing sessions for mirako_session_id=%s count=%s details=%s",
                mirako_session_id,
                len(existing_for_mirako),
                [
                    {
                        "session_id": session.session_id,
                        "closed": session.closed,
                        "closed_reason": session.closed_reason,
                        "gateway_stopped": session.gateway_stopped,
                        "recall_bot_id": session.recall_bot_id,
                    }
                    for session in existing_for_mirako
                ],
            )

        public_base_url = self._normalize_public_base_url()
        session_id = secrets.token_urlsafe(24)
        bridge_url = f"{public_base_url}/bridge/{session_id}"

        created_meeting: dict[str, Any] | None = None
        meeting_url = req.meeting_url
        if not meeting_url:
            logger.info(
                "meeting_url omitted; creating meeting via provider=%s mirako_session_id=%s",
                req.meeting_provider,
                mirako_session_id,
            )
            created_meeting = await self._create_meeting(req)
            meeting_url = str(created_meeting.get("join_url") or "")
            if not meeting_url:
                raise SessionServiceError(
                    502,
                    {
                        "error": f"{req.meeting_provider}_join_url_missing",
                        req.meeting_provider: created_meeting,
                    },
                )
            logger.info(
                "meeting created provider=%s meeting_id=%s mirako_session_id=%s",
                req.meeting_provider,
                created_meeting.get("id"),
                mirako_session_id,
            )
        else:
            logger.info(
                "using provided meeting_url provider=%s mirako_session_id=%s",
                req.meeting_provider,
                mirako_session_id,
            )

        initial_conversation_mode = self._initial_conversation_mode()
        bot_name = req.bot_name or self.settings.bot_name
        self.sessions[session_id] = Session(
            session_id=session_id,
            created_at=time.time(),
            mirako_session_id=mirako_session_id,
            gateway_url=gateway_url,
            mode=req.mode,
            meeting_provider=req.meeting_provider,
            meeting_url=meeting_url,
            bridge_url=bridge_url,
            bot_name=bot_name,
            recall_bot_id="",
            recall_bot={},
            transcript_utterances=[],
            meeting_participants={},
            meeting_participant_count=0,
            last_non_bot_participant_left_at=None,
            has_seen_non_bot_participant=False,
            bot_only_cleanup_started=False,
            conversation_mode=initial_conversation_mode,
            created_meeting=created_meeting,
            should_end_created_meeting=bool(created_meeting),
        )
        recall_store.upsert_session(
            session_id=session_id,
            mirako_session_id=mirako_session_id,
            meeting_provider=req.meeting_provider,
            meeting_url=meeting_url,
            mode=req.mode,
            bridge_url=bridge_url,
            created_meeting=created_meeting,
        )
        logger.info(
            "session stored session_id=%s mirako_session_id=%s provider=%s mode=%s conversation_mode_policy=%s conversation_mode=%s bridge_url=%s",
            session_id,
            mirako_session_id,
            req.meeting_provider,
            req.mode,
            self.settings.conversation_mode_policy,
            initial_conversation_mode,
            bridge_url,
        )

        try:
            bot = await self._create_recall_bot(
                req, meeting_url=meeting_url, bridge_url=bridge_url
            )
        except SessionServiceError:
            self.sessions.pop(session_id, None)
            await self._best_effort_end_created_meeting(
                req.meeting_provider, created_meeting
            )
            logger.warning(
                "session rolled back after recall bot creation failure session_id=%s",
                session_id,
            )
            raise

        session = self.sessions[session_id]
        session.recall_bot = bot
        session.recall_bot_id = str(bot.get("id") or bot.get("bot_id") or "")
        if not session.recall_bot_id:
            self.sessions.pop(session_id, None)
            await self._best_effort_end_created_meeting(
                req.meeting_provider, created_meeting
            )
            raise SessionServiceError(
                502, {"error": "recall_bot_id_missing", "bot": bot}
            )
        recall_store.upsert_session(
            session_id=session_id,
            mirako_session_id=mirako_session_id,
            recall_bot_id=session.recall_bot_id,
            recall_bot=bot,
        )
        await self._start_realtime_capture(session_id)
        recall_store.add_event(
            event_type="bot.create",
            session_id=session_id,
            mirako_session_id=mirako_session_id,
            recall_bot_id=session.recall_bot_id,
            payload=bot,
        )

        return CreateSessionResponse(
            session_id=session_id,
            meeting_url=meeting_url,
            mirako_session_id=mirako_session_id,
            bridge_url=bridge_url,
            mode=req.mode,
            meeting_provider=req.meeting_provider,
            created_meeting=created_meeting,
            recall_bot_id=session.recall_bot_id,
            recall_bot=bot,
        )

    async def handle_recall_transcript(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            event = str(payload.get("event") or "")
            if event != "transcript.data":
                logger.info("recall transcript webhook ignored event=%s", event)
                return {"ok": True, "ignored": True}

            session = self._session_from_recall_payload(payload)
            if session is None:
                logger.warning(
                    "recall transcript webhook unknown session payload=%s", payload
                )
                return {"ok": True, "ignored": True, "reason": "unknown_session"}
            recall_store.add_event(
                event_type=event,
                session_id=session.session_id,
                mirako_session_id=session.mirako_session_id,
                recall_bot_id=session.recall_bot_id,
                payload=payload,
            )

            transcript_data = (payload.get("data") or {}).get("data") or {}
            words = transcript_data.get("words") or []
            content = " ".join(
                str(word.get("text") or "").strip()
                for word in words
                if isinstance(word, dict)
            ).strip()
            if not content:
                return {"ok": True, "ignored": True, "reason": "empty_content"}

            participant = transcript_data.get("participant") or {}
            speaker = str(
                participant.get("name")
                or participant.get("email")
                or participant.get("id")
                or "default"
            )
            utterance = {
                "session_id": session.session_id,
                "mirako_session_id": session.mirako_session_id,
                "content": content,
                "speaker": speaker,
                "participant": participant,
                "language_code": transcript_data.get("language_code"),
                "words": words,
            }
            session.transcript_utterances.append(utterance)
            start_time, end_time = self._transcript_time_range(transcript_data, words)
            recall_store.add_meeting_record(
                session_id=session.session_id,
                mirako_session_id=session.mirako_session_id,
                recall_bot_id=session.recall_bot_id,
                speaker=speaker,
                content=content,
                start_time=start_time,
                end_time=end_time,
                participant=participant,
                words=words,
                language_code=transcript_data.get("language_code"),
                source_event=event,
                payload=payload,
            )
            logger.info(
                "recall transcript stored session_id=%s speaker=%s language_code=%s words=%s chars=%s start_time=%s end_time=%s content_preview=%s",
                session.session_id,
                speaker,
                transcript_data.get("language_code"),
                len(words),
                len(content),
                start_time,
                end_time,
                content[:120],
            )
            return {"ok": True, "stored": True}
        except Exception:
            logger.exception("recall transcript processing failed")
            return {"ok": False}

    async def handle_recall_participant_event(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            event = str(payload.get("event") or "")
            if event not in {
                "participant_events.join",
                "participant_events.leave",
                "participant_events.update",
                "participant_events.speech_on",
                "participant_events.speech_off",
                "participant_events.screenshare_on",
                "participant_events.screenshare_off",
                "participant_events.chat_message",
            }:
                logger.info("recall participant webhook ignored event=%s", event)
                return {"ok": True, "ignored": True}

            session = self._session_from_recall_payload(payload)
            if session is None:
                logger.warning(
                    "recall participant webhook unknown session payload=%s", payload
                )
                return {"ok": True, "ignored": True, "reason": "unknown_session"}
            recall_store.add_event(
                event_type=event,
                session_id=session.session_id,
                mirako_session_id=session.mirako_session_id,
                recall_bot_id=session.recall_bot_id,
                payload=payload,
            )

            participant = ((payload.get("data") or {}).get("data") or {}).get(
                "participant"
            ) or {}
            participant_event_data = (payload.get("data") or {}).get("data") or {}
            chat_data = participant_event_data.get("data") or {}
            timestamp = participant_event_data.get("timestamp") or {}
            participant_id = self._participant_key(participant)
            if not participant_id:
                logger.warning(
                    "recall participant webhook missing participant id event=%s payload=%s",
                    event,
                    payload,
                )
                return {"ok": True, "ignored": True, "reason": "missing_participant_id"}

            if event == "participant_events.chat_message":
                logger.info(
                    "recall chat message received session_id=%s mirako_session_id=%s participant_id=%s participant_name=%s participant_email=%s chat_to=%s text=%s timestamp_absolute=%s timestamp_relative=%s",
                    session.session_id,
                    session.mirako_session_id,
                    participant_id,
                    participant.get("name"),
                    participant.get("email"),
                    chat_data.get("to") if isinstance(chat_data, dict) else None,
                    chat_data.get("text") if isinstance(chat_data, dict) else None,
                    timestamp.get("absolute") if isinstance(timestamp, dict) else None,
                    timestamp.get("relative") if isinstance(timestamp, dict) else None,
                )
                bot_mentioned, command_text = self._parse_bot_mention(
                    chat_data.get("text") if isinstance(chat_data, dict) else None,
                    session.bot_name,
                )
                logger.info(
                    "recall chat command parsed session_id=%s mirako_session_id=%s bot_name=%s bot_mentioned=%s command_text=%s",
                    session.session_id,
                    session.mirako_session_id,
                    session.bot_name,
                    bot_mentioned,
                    command_text,
                )
                routed_to_gateway = await self._handle_gateway_chat_command(
                    session,
                    bot_mentioned=bot_mentioned,
                    command_text=command_text,
                )
                if not routed_to_gateway:
                    await self._handle_agent_chat_message(
                        session,
                        bot_mentioned=bot_mentioned,
                        command_text=command_text,
                    )

            if event == "participant_events.leave":
                session.meeting_participants.pop(participant_id, None)
                session.sharing_participant_ids.discard(participant_id)
            else:
                session.meeting_participants[participant_id] = participant
                if event == "participant_events.screenshare_on":
                    session.sharing_participant_ids.add(participant_id)
                    session.last_sharing_participant_id = participant_id
                    session.last_sharing_event_at = time.time()
                elif event == "participant_events.screenshare_off":
                    session.sharing_participant_ids.discard(participant_id)

            self._refresh_participant_state(session)
            desired_mode = self._desired_conversation_mode(
                session.meeting_participant_count
            )
            if (
                event == "participant_events.leave"
                and session.meeting_participant_count == 0
            ):
                await self._best_effort_stop_gateway_session(
                    session, reason="all_participants_left"
                )

            if desired_mode is None:
                logger.info(
                    "participant event processed without mode switch session_id=%s event=%s participant_id=%s count=%s policy=%s current_mode=%s",
                    session.session_id,
                    event,
                    participant_id,
                    session.meeting_participant_count,
                    self.settings.conversation_mode_policy,
                    session.conversation_mode,
                )
                return {
                    "ok": True,
                    "participant_count": session.meeting_participant_count,
                    "conversation_mode": session.conversation_mode,
                }
            mode_changed = desired_mode != session.conversation_mode
            logger.info(
                "participant event processed session_id=%s event=%s participant_id=%s count=%s desired_mode=%s current_mode=%s mode_changed=%s",
                session.session_id,
                event,
                participant_id,
                session.meeting_participant_count,
                desired_mode,
                session.conversation_mode,
                mode_changed,
            )
            if mode_changed:
                session.conversation_mode = desired_mode
                await self._set_gateway_conversation_mode(session, desired_mode)

            return {
                "ok": True,
                "participant_count": session.meeting_participant_count,
                "conversation_mode": session.conversation_mode,
            }
        except Exception:
            logger.exception("recall participant event processing failed")
            return {"ok": False}

    async def handle_recall_bot_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            event = str(payload.get("event") or "")
            if event not in {"bot.call_ended", "bot.done", "bot.fatal"}:
                logger.info("recall bot status webhook ignored event=%s", event)
                return {"ok": True, "ignored": True}

            session = self._session_from_recall_payload(payload)
            if session is None:
                logger.warning(
                    "recall bot status webhook unknown session event=%s payload=%s",
                    event,
                    payload,
                )
                return {"ok": True, "ignored": True, "reason": "unknown_session"}

            recall_store.add_event(
                event_type=event,
                session_id=session.session_id,
                mirako_session_id=session.mirako_session_id,
                recall_bot_id=session.recall_bot_id,
                payload=payload,
            )

            self._mark_session_closed(session, reason=event)
            recall_store.upsert_session(
                session_id=session.session_id,
                mirako_session_id=session.mirako_session_id,
                recall_bot_id=session.recall_bot_id,
                closed_at=time.time(),
            )

            gateway_stopped = False
            try:
                logger.info(
                    "recall bot terminal event stopping gateway session_id=%s mirako_session_id=%s event=%s",
                    session.session_id,
                    session.mirako_session_id,
                    event,
                )
                await self._stop_gateway_session(session)
                gateway_stopped = True
                session.gateway_stopped = True
            except Exception:
                logger.exception(
                    "recall bot terminal event failed to stop gateway session_id=%s mirako_session_id=%s event=%s",
                    session.session_id,
                    session.mirako_session_id,
                    event,
                )

            return {"ok": True, "event": event, "gateway_stopped": gateway_stopped}
        except Exception:
            logger.exception("recall bot status processing failed")
            return {"ok": False}

    async def close_session(self, session_id: str) -> CloseSessionResponse:
        session = self.sessions.get(session_id)
        if session is None:
            raise SessionServiceError(404, "Unknown session_id.")
        self._mark_session_closed(session, reason="api.close_session")

        recall_response = None
        recall_left = False
        if session.recall_bot_id:
            try:
                logger.info(
                    "leaving recall bot session_id=%s recall_bot_id=%s",
                    session_id,
                    session.recall_bot_id,
                )
                recall_response = await self._recall_client().leave_call(
                    session.recall_bot_id
                )
                recall_left = True
                recall_store.add_event(
                    event_type="bot.leave_call",
                    session_id=session.session_id,
                    mirako_session_id=session.mirako_session_id,
                    recall_bot_id=session.recall_bot_id,
                    payload=recall_response,
                )
            except Exception as exc:
                raise SessionServiceError(
                    502, self._http_error_detail(exc, service="recall")
                )
        recall_store.upsert_session(
            session_id=session.session_id,
            mirako_session_id=session.mirako_session_id,
            recall_bot_id=session.recall_bot_id,
            closed_at=time.time(),
        )

        gateway_response = None
        gateway_stopped = False
        try:
            logger.info(
                "stopping gateway session_id=%s mirako_session_id=%s gateway_url=%s",
                session_id,
                session.mirako_session_id,
                session.gateway_url,
            )
            gateway_response = await self._stop_gateway_session(session)
            gateway_stopped = True
            session.gateway_stopped = True
        except Exception as exc:
            raise SessionServiceError(
                502, self._http_error_detail(exc, service="gateway")
            )

        zoom_response = None
        zoom_ended = False
        if session.should_end_created_meeting and session.created_meeting:
            meeting_id = session.created_meeting.get("id")
            if meeting_id:
                try:
                    logger.info(
                        "ending provider-created meeting session_id=%s provider=%s meeting_id=%s",
                        session_id,
                        session.meeting_provider,
                        meeting_id,
                    )
                    zoom_response = await self._meeting_strategy(
                        session.meeting_provider
                    ).end_meeting(meeting_id)
                    zoom_ended = True
                except Exception as exc:
                    raise SessionServiceError(
                        502,
                        self._http_error_detail(exc, service=session.meeting_provider),
                    )

        await self._stop_realtime_capture(session_id)

        return CloseSessionResponse(
            session_id=session_id,
            recall_left=recall_left,
            recall_response=recall_response,
            gateway_stopped=gateway_stopped,
            gateway_response=gateway_response,
            zoom_ended=zoom_ended,
            zoom_response=zoom_response,
        )

    async def list_participants(self, session_id: str) -> ListParticipantsResponse:
        session = self.get_session(session_id)
        if session is None:
            raise SessionServiceError(404, "Unknown session_id.")
        if session.closed:
            logger.info(
                "list_participants on closed session_id=%s reason=%s",
                session_id,
                session.closed_reason,
            )
        cache = self._frame_caches.get(session_id)
        cached: set[str] = set()
        if cache is not None:
            for item in await cache.list_participants():
                pid = item.get("participant_id")
                if pid:
                    cached.add(str(pid))
        participants: list[ParticipantInfo] = []
        for pid, info in session.meeting_participants.items():
            participants.append(
                ParticipantInfo(
                    participant_id=pid,
                    name=str(info.get("name") or ""),
                    is_host=bool(info.get("is_host")),
                    email=info.get("email"),
                    is_screensharing=pid in session.sharing_participant_ids,
                    has_cached_frame=pid in cached,
                )
            )
        participants.sort(
            key=lambda p: (not p.is_screensharing, p.name.lower(), p.participant_id)
        )
        return ListParticipantsResponse(
            session_id=session_id,
            mirako_session_id=session.mirako_session_id,
            participants=participants,
            last_sharing_participant_id=session.last_sharing_participant_id,
        )

    async def capture_screen(
        self,
        session_id: str,
        req: CaptureScreenRequest,
    ) -> CaptureScreenResponse:
        session = self.get_session(session_id)
        if session is None:
            raise SessionServiceError(404, "Unknown session_id.")
        if not self.settings.recall_video_separate_h264_enabled:
            raise SessionServiceError(
                400,
                {
                    "error": "video_separate_h264_disabled",
                    "message": "RECALL_VIDEO_SEPARATE_H264_ENABLED is false.",
                },
            )
        cache = self._frame_caches.get(session_id)
        if cache is None:
            raise SessionServiceError(
                503,
                {
                    "error": "frame_cache_not_ready",
                    "message": "H264 frame cache is not initialized for this session.",
                },
            )
        frame_type = (
            req.frame_type
            if req.frame_type in {"webcam", "screenshare"}
            else "screenshare"
        )
        cached: CachedFrame | None
        target_pid: str | None
        if req.participant_id:
            cached = await cache.get_frame(
                participant_id=req.participant_id, frame_type=frame_type
            )
            target_pid = req.participant_id
        else:
            cached = await cache.latest_frame(frame_type=frame_type)
            target_pid = cached.participant_id if cached else None
        if cached is None:
            if req.participant_id:
                raise SessionServiceError(
                    404,
                    {
                        "error": "no_cached_frame",
                        "participant_id": req.participant_id,
                        "frame_type": frame_type,
                    },
                )
            raise SessionServiceError(
                404,
                {
                    "error": "no_cached_frame",
                    "message": f"No cached {frame_type} frame available yet.",
                },
            )
        try:
            png_bytes = self._decode_idr_to_png(cached)
        except Exception as exc:
            logger.exception(
                "capture_screen decode failed session_id=%s participant_id=%s",
                session_id,
                target_pid,
            )
            raise SessionServiceError(
                502,
                {
                    "error": "decode_failed",
                    "message": str(exc),
                },
            ) from exc
        participant_name = ""
        if target_pid:
            info = session.meeting_participants.get(target_pid)
            if info:
                participant_name = str(info.get("name") or "")
        description = await self._describe_with_minimax(
            png_bytes=png_bytes,
            prompt=req.prompt or self.settings.capture_prompt,
        )
        return CaptureScreenResponse(
            session_id=session_id,
            mirako_session_id=session.mirako_session_id,
            participant_id=target_pid or "",
            participant_name=participant_name,
            frame_type=frame_type,
            received_at=cached.received_at,
            captured_at=time.time(),
            frame_age_ms=int(max(0.0, (time.time() - cached.received_at) * 1000)),
            width=cached.width,
            height=cached.height,
            description=description,
            image_base64=(_b64encode(png_bytes) if req.include_image_base64 else None),
            mime_type="image/png",
        )

    def _normalize_public_base_url(self) -> str:
        base = self.settings.public_base_url.rstrip("/")
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

    async def _create_meeting(self, req: CreateSessionRequest) -> dict[str, Any]:
        if req.meeting_provider == "google_meet":
            raise SessionServiceError(
                400,
                {
                    "error": "google_meet_strategy_not_implemented",
                    "message": "meeting_provider=google_meet is reserved, but automatic Google Meet creation is not implemented yet. Provide meeting_url with meeting_provider=zoom, or implement the Google Meet strategy.",
                },
            )
        try:
            logger.info("creating meeting provider=%s", req.meeting_provider)
            return await self._meeting_strategy(req.meeting_provider).create_meeting(
                req
            )
        except Exception as exc:
            logger.exception("create meeting failed provider=%s", req.meeting_provider)
            raise SessionServiceError(
                502, self._http_error_detail(exc, service=req.meeting_provider)
            )

    async def _create_recall_bot(
        self, req: CreateSessionRequest, *, meeting_url: str, bridge_url: str
    ) -> dict[str, Any]:
        try:
            logger.info(
                "creating recall bot provider=%s mode=%s bridge_url=%s transcript_enabled=%s transcript_mode=%s transcript_language_code=%s signed_in_zoom=%s",
                req.meeting_provider,
                req.mode,
                bridge_url,
                self.settings.recall_transcript_enabled,
                self.settings.recall_transcript_mode,
                self.settings.recall_transcript_language_code,
                self.settings.zoom_signed_in_enabled,
            )
            return await self._recall_client().create_bot(
                meeting_url=meeting_url,
                bot_name=req.bot_name or self.settings.bot_name,
                variant=self.settings.recall_bot_variant,
                output_media_url=bridge_url,
                metadata={
                    "session_id": bridge_url.rsplit("/", 1)[-1],
                    "mirako_session_id": req.mirako_session_id,
                },
                recording_config=self._recall_recording_config(req, bridge_url),
                automatic_leave=self._recall_automatic_leave_config(),
                zoom=self._recall_zoom_config(req, bridge_url),
            )
        except httpx.HTTPStatusError as exc:
            logger.exception(
                "recall bot creation failed status_code=%s", exc.response.status_code
            )
            raise SessionServiceError(
                exc.response.status_code, self._http_error_detail(exc, service="recall")
            )
        except Exception as exc:
            logger.exception("recall bot creation failed")
            raise SessionServiceError(
                502, self._http_error_detail(exc, service="recall")
            )

    async def _stop_gateway_session(self, session: Session) -> dict[str, Any] | None:
        if session.gateway_stopped:
            logger.info(
                "skip gateway stop; already stopped session_id=%s mirako_session_id=%s closed=%s closed_reason=%s",
                session.session_id,
                session.mirako_session_id,
                session.closed,
                session.closed_reason,
            )
            return {
                "status_code": 200,
                "message": "Gateway session was already stopped by recall_tools.",
            }
        async with httpx.AsyncClient(timeout=15) as client:
            logger.info(
                "posting gateway stop session_id=%s mirako_session_id=%s closed=%s closed_reason=%s gateway_url=%s",
                session.session_id,
                session.mirako_session_id,
                session.closed,
                session.closed_reason,
                session.gateway_url,
            )
            response = await client.post(
                f"{session.gateway_url}/api/sessions/{session.mirako_session_id}/stop",
                headers={"Accept": "application/json"},
            )
            logger.info(
                "gateway stop response session_id=%s mirako_session_id=%s status_code=%s body=%s",
                session.session_id,
                session.mirako_session_id,
                response.status_code,
                response.text[:500],
            )
            if response.status_code == 404:
                logger.warning(
                    "gateway session not found mirako_session_id=%s",
                    session.mirako_session_id,
                )
                session.gateway_stopped = True
                return {
                    "status_code": 404,
                    "message": "Gateway session was already stopped or not found.",
                }
            response.raise_for_status()
            session.gateway_stopped = True
            if not response.content:
                return None
            try:
                return response.json()
            except ValueError:
                return {"status_code": response.status_code, "response": response.text}

    async def _handle_gateway_chat_command(
        self,
        session: Session,
        *,
        bot_mentioned: bool,
        command_text: str,
    ) -> bool:
        if not self.settings.chat_gateway_commands_enabled:
            return False
        if not bot_mentioned:
            return False
        command = (
            command_text.strip().split(maxsplit=1)[0].lower()
            if command_text.strip()
            else ""
        )
        if command not in {"start", "pause"}:
            logger.info(
                "recall chat command not routed to gateway session_id=%s mirako_session_id=%s command=%s",
                session.session_id,
                session.mirako_session_id,
                command,
            )
            return False
        async with httpx.AsyncClient(timeout=5) as client:
            logger.info(
                "posting gateway assistant state from chat session_id=%s mirako_session_id=%s command=%s gateway_url=%s",
                session.session_id,
                session.mirako_session_id,
                command,
                session.gateway_url,
            )
            response = await client.post(
                f"{session.gateway_url}/api/sessions/{session.mirako_session_id}/assistant-state",
                headers={"Accept": "application/json"},
                json={"state": command},
            )
            logger.info(
                "gateway assistant state response session_id=%s mirako_session_id=%s command=%s status_code=%s body=%s",
                session.session_id,
                session.mirako_session_id,
                command,
                response.status_code,
                response.text[:500],
            )
            response.raise_for_status()
            return True

    async def _handle_agent_chat_message(
        self,
        session: Session,
        *,
        bot_mentioned: bool,
        command_text: str,
    ) -> None:
        if not self.settings.chat_agent_enabled:
            return
        if not bot_mentioned:
            return
        message = command_text.strip()
        if not message:
            return
        client = HermesAgentClient(
            api_url=self.settings.hermes_agent_api_url,
            api_key=self.settings.hermes_agent_api_key,
            model=self.settings.hermes_agent_model,
            timeout_seconds=self.settings.hermes_agent_timeout_seconds,
        )
        chunks: list[str] = []
        logger.info(
            "posting recall chat message to hermes agent session_id=%s mirako_session_id=%s recall_session_id=%s chars=%s api_url=%s model=%s",
            session.session_id,
            session.mirako_session_id,
            session.session_id,
            len(message),
            self.settings.hermes_agent_api_url,
            self.settings.hermes_agent_model,
        )
        try:
            async for chunk in client.chat(session_id=session.session_id, message=message):
                chunks.append(chunk)
        except HermesAgentError as exc:
            logger.error(
                "hermes agent chat failed session_id=%s mirako_session_id=%s status_code=%s detail=%s",
                session.session_id,
                session.mirako_session_id,
                exc.status_code,
                exc.detail[:1000],
            )
            return
        response_text = "".join(chunks).strip()
        if not response_text:
            logger.info(
                "hermes agent returned empty response session_id=%s mirako_session_id=%s",
                session.session_id,
                session.mirako_session_id,
            )
            return
        await self._send_recall_chat_reply(session, response_text)

    async def _send_recall_chat_reply(self, session: Session, message: str) -> None:
        if not session.recall_bot_id:
            return
        text = message[:4096]
        try:
            await self._recall_client().send_chat_message(
                session.recall_bot_id,
                message=text,
                to="everyone",
            )
        except Exception:
            logger.exception(
                "recall chat reply failed session_id=%s mirako_session_id=%s recall_bot_id=%s chars=%s",
                session.session_id,
                session.mirako_session_id,
                session.recall_bot_id,
                len(text),
            )

    async def _set_gateway_conversation_mode(self, session: Session, mode: str) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{session.gateway_url}/api/sessions/{session.mirako_session_id}/mode",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json={"mode": mode},
            )
            if response.status_code >= 400:
                logger.error(
                    "gateway conversation mode failed session_id=%s gateway_session_id=%s mode=%s status_code=%s response=%s",
                    session.session_id,
                    session.mirako_session_id,
                    mode,
                    response.status_code,
                    response.text,
                )
            response.raise_for_status()

    def _recall_recording_config(
        self, req: CreateSessionRequest, bridge_url: str
    ) -> dict[str, Any] | None:
        if not self.settings.recall_transcript_enabled:
            return None
        public_base_url = bridge_url.rsplit("/bridge/", 1)[0]
        session_id = bridge_url.rsplit("/", 1)[-1]
        participant_events = [
            "participant_events.join",
            "participant_events.leave",
            "participant_events.update",
            "participant_events.speech_on",
            "participant_events.speech_off",
            "participant_events.screenshare_on",
            "participant_events.screenshare_off",
            "participant_events.chat_message",
        ]
        config: dict[str, Any] = {
            "transcript": {
                "provider": {
                    "recallai_streaming": {
                        "mode": self.settings.recall_transcript_mode,
                        "language_code": self.settings.recall_transcript_language_code,
                    }
                },
                "diarization": {"use_separate_streams_when_available": True},
            },
            "realtime_endpoints": [
                {
                    "type": "webhook",
                    "url": f"{public_base_url}/api/recall/transcript",
                    "events": ["transcript.data"],
                    "metadata": {
                        "session_id": session_id,
                        "mirako_session_id": req.mirako_session_id,
                    },
                },
                {
                    "type": "webhook",
                    "url": f"{public_base_url}/api/recall/participant-events",
                    "events": participant_events,
                    "metadata": {
                        "session_id": session_id,
                        "mirako_session_id": req.mirako_session_id,
                    },
                },
            ],
        }
        if self.settings.recall_video_separate_h264_enabled:
            config["video_separate_h264"] = {}
            config["video_mixed_layout"] = self.settings.recall_video_mixed_layout
        logger.info(
            "recall recording config built session_id=%s transcript_mode=%s transcript_language_code=%s transcript_webhook_events=%s participant_webhook_events=%s video_separate_h264=%s video_mixed_layout=%s",
            session_id,
            self.settings.recall_transcript_mode,
            self.settings.recall_transcript_language_code,
            ["transcript.data"],
            participant_events,
            self.settings.recall_video_separate_h264_enabled,
            config.get("video_mixed_layout"),
        )
        return config

    def _recall_automatic_leave_config(self) -> dict[str, Any]:
        return {
            "everyone_left_timeout": {
                "timeout": max(1, self.settings.recall_everyone_left_timeout_seconds),
                "activate_after": max(
                    1, self.settings.recall_everyone_left_activate_after_seconds
                ),
            }
        }

    def _recall_zoom_config(
        self, req: CreateSessionRequest, bridge_url: str
    ) -> dict[str, Any] | None:
        if req.meeting_provider != "zoom" or not self.settings.zoom_signed_in_enabled:
            return None
        public_base_url = bridge_url.rsplit("/bridge/", 1)[0]
        secret = self.settings.zoom_zak_callback_secret
        if not secret:
            raise SessionServiceError(
                400,
                {
                    "error": "zoom_zak_callback_secret_required",
                    "message": "Set ZOOM_ZAK_CALLBACK_SECRET or SERVICE_API_KEY before enabling signed-in Zoom bots.",
                },
            )
        return {"zak_url": f"{public_base_url}/api/zoom/zak?secret={secret}"}

    async def _start_realtime_capture(self, session_id: str) -> None:
        if not self.settings.recall_video_separate_h264_enabled:
            return
        session = self.sessions.get(session_id)
        if session is None or not session.recall_bot_id:
            return
        cache = self._frame_caches.get(session_id)
        if cache is None:
            cache = H264FrameCache(
                max_participants=self.settings.frame_cache_max_participants,
                max_age_seconds=self.settings.frame_cache_max_age_seconds,
                max_bytes=self.settings.frame_cache_max_bytes,
            )
            self._frame_caches[session_id] = cache
        if (
            session_id in self._realtime_clients
            and self._realtime_clients[session_id].is_running()
        ):
            return
        client = RecallRealtimeClient(
            ws_url=self.settings.recall_realtime_ws_url,
            api_key=self.settings.recall_api_key,
            bot_id=session.recall_bot_id,
            events=[H264_EVENT],
            on_event=self._build_h264_handler(session_id, cache),
        )
        self._realtime_clients[session_id] = client
        client.start()
        logger.info(
            "recall realtime capture started session_id=%s bot_id=%s ws_url=%s",
            session_id,
            session.recall_bot_id,
            self.settings.recall_realtime_ws_url,
        )

    async def _stop_realtime_capture(self, session_id: str) -> None:
        client = self._realtime_clients.pop(session_id, None)
        if client is not None:
            try:
                await client.stop()
            except Exception:
                logger.exception(
                    "recall realtime capture stop failed session_id=%s", session_id
                )
        cache = self._frame_caches.pop(session_id, None)
        if cache is not None:
            try:
                await cache.clear()
            except Exception:
                logger.exception("frame cache clear failed session_id=%s", session_id)
        logger.info("recall realtime capture stopped session_id=%s", session_id)

    def _build_h264_handler(self, session_id: str, cache: H264FrameCache):
        async def handle(event: str, data: dict[str, Any]) -> None:
            if event != H264_EVENT:
                return
            frame_type = str(data.get("type") or "")
            buffer_b64 = str(data.get("buffer") or "")
            participant = data.get("participant") or {}
            pid = participant.get("id")
            if pid is None:
                return
            await cache.ingest(
                participant_id=str(pid),
                frame_type=frame_type,
                buffer_b64=buffer_b64,
            )
            session = self.sessions.get(session_id)
            if session is not None and frame_type == "screenshare":
                session.last_sharing_participant_id = str(pid)
                session.last_sharing_event_at = time.time()

        return handle

    def _decode_idr_to_png(self, frame: CachedFrame) -> bytes:
        container = av.open(io.BytesIO(frame.annexb_bytes), format="h264")
        stream = container.streams.video[0]
        for packet in container.demux(stream):
            for decoded in packet.decode():
                width = decoded.width
                height = decoded.height
                frame.width = width
                frame.height = height
                img = decoded.to_image()
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                container.close()
                return buf.getvalue()
        container.close()
        raise RuntimeError("No decodable frame in cached H264 buffer.")

    async def _describe_with_minimax(self, *, png_bytes: bytes, prompt: str) -> str:
        if not self.settings.minimax_api_key:
            return ""
        client = MinimaxVisionClient(
            api_base=self.settings.minimax_api_base,
            api_key=self.settings.minimax_api_key,
            model=self.settings.minimax_model,
            timeout_seconds=self.settings.minimax_timeout_seconds,
        )
        try:
            return await client.describe_image(
                image_bytes=png_bytes,
                mime_type=normalize_mime(png_bytes),
                prompt=prompt,
            )
        except MinimaxVisionError as exc:
            logger.error(
                "minimax vision call failed status=%s detail=%s",
                exc.status_code,
                exc.detail,
            )
            return ""

    def _refresh_participant_state(self, session: Session) -> None:
        non_bot_participants = {
            key: participant
            for key, participant in session.meeting_participants.items()
            if not self._is_recall_bot_participant(participant)
        }
        session.meeting_participant_count = len(non_bot_participants)
        now = time.time()
        if session.meeting_participant_count > 0:
            session.has_seen_non_bot_participant = True
            session.last_non_bot_participant_left_at = None
            session.bot_only_cleanup_started = False
        elif (
            session.has_seen_non_bot_participant
            and session.last_non_bot_participant_left_at is None
        ):
            session.last_non_bot_participant_left_at = now

    def _is_recall_bot_participant(self, participant: dict[str, Any]) -> bool:
        name = str(participant.get("name") or "").strip().lower()
        bot_name = self.settings.bot_name.strip().lower()
        return bool(bot_name and name == bot_name)

    def _session_bot_name(self, session: Session) -> str:
        return (session.bot_name or self.settings.bot_name).strip()

    @staticmethod
    def _parse_bot_mention(text: object, bot_name: str) -> tuple[bool, str]:
        content = str(text or "").strip()
        mention = f"@{bot_name.strip()}"
        if not content or not mention.strip():
            return False, content
        if not content.lower().startswith(mention.lower()):
            return False, content
        return True, content[len(mention) :].strip()

    async def cleanup_bot_only_sessions(self) -> None:
        if not self.settings.bot_only_cleanup_enabled:
            return
        now = time.time()
        timeout = max(1, self.settings.bot_only_cleanup_seconds)
        sessions = list(self.sessions.values())
        for session in sessions:
            self._refresh_participant_state(session)
            if session.gateway_stopped or session.bot_only_cleanup_started:
                continue
            if not session.has_seen_non_bot_participant:
                continue
            left_at = session.last_non_bot_participant_left_at
            if left_at is None or now - left_at < timeout:
                continue
            session.bot_only_cleanup_started = True
            logger.warning(
                "bot-only cleanup triggered session_id=%s mirako_session_id=%s recall_bot_id=%s idle_seconds=%.1f participant_count=%s",
                session.session_id,
                session.mirako_session_id,
                session.recall_bot_id,
                now - left_at,
                session.meeting_participant_count,
            )
            recall_store.add_event(
                event_type="bot_only_cleanup",
                session_id=session.session_id,
                mirako_session_id=session.mirako_session_id,
                recall_bot_id=session.recall_bot_id,
                payload={
                    "reason": "bot_only_timeout",
                    "timeout_seconds": timeout,
                    "participant_count": session.meeting_participant_count,
                },
            )
            await self._best_effort_leave_recall_bot(session.recall_bot_id)
            await self._best_effort_stop_gateway_session(
                session, reason="bot_only_timeout"
            )
            self.sessions.pop(session.session_id, None)
            recall_store.upsert_session(
                session_id=session.session_id,
                mirako_session_id=session.mirako_session_id,
                recall_bot_id=session.recall_bot_id,
                closed_at=time.time(),
            )

    @staticmethod
    def _participant_key(participant: dict[str, Any]) -> str:
        participant_id = participant.get("id")
        if participant_id is not None:
            return str(participant_id)
        email = participant.get("email")
        if email:
            return f"email:{email}"
        name = participant.get("name")
        if name:
            return f"name:{name}"
        return ""

    def _transcript_time_range(
        self,
        transcript_data: dict[str, Any],
        words: list[Any],
    ) -> tuple[float | None, float | None]:
        start_time = self._first_number(
            transcript_data,
            ("start_time", "start_timestamp", "start", "timestamp", "audio_start_time"),
        )
        end_time = self._first_number(
            transcript_data,
            ("end_time", "end_timestamp", "end", "audio_end_time"),
        )
        word_starts: list[float] = []
        word_ends: list[float] = []
        for word in words:
            if not isinstance(word, dict):
                continue
            word_start = self._first_number(
                word, ("start_time", "start_timestamp", "start", "timestamp")
            )
            word_end = self._first_number(word, ("end_time", "end_timestamp", "end"))
            if word_start is not None:
                word_starts.append(word_start)
            if word_end is not None:
                word_ends.append(word_end)
        if start_time is None and word_starts:
            start_time = min(word_starts)
        if end_time is None and word_ends:
            end_time = max(word_ends)
        return start_time, end_time

    @staticmethod
    def _first_number(data: dict[str, Any], keys: tuple[str, ...]) -> float | None:
        for key in keys:
            value = data.get(key)
            if isinstance(value, int | float):
                return float(value)
            if isinstance(value, str):
                try:
                    return float(value)
                except ValueError:
                    continue
        return None

    def _initial_conversation_mode(self) -> str:
        policy = self.settings.conversation_mode_policy
        if policy in {"multi", "single"}:
            return policy
        return "multi"

    def _mark_session_closed(self, session: Session, *, reason: str) -> None:
        if session.closed:
            logger.info(
                "session already marked closed session_id=%s mirako_session_id=%s existing_reason=%s new_reason=%s",
                session.session_id,
                session.mirako_session_id,
                session.closed_reason,
                reason,
            )
            return
        session.closed = True
        session.closed_reason = reason
        session.closed_at = time.time()
        logger.info(
            "session marked closed; bridge reconnects will be blocked session_id=%s mirako_session_id=%s reason=%s gateway_stopped=%s closed_at=%s",
            session.session_id,
            session.mirako_session_id,
            reason,
            session.gateway_stopped,
            session.closed_at,
        )

    def _desired_conversation_mode(self, participant_count: int) -> str | None:
        policy = self.settings.conversation_mode_policy
        if policy in {"multi", "single"}:
            return None
        return "multi" if participant_count > 2 else "single"

    def _session_from_recall_payload(self, payload: dict[str, Any]) -> Session | None:
        data = payload.get("data") or {}
        realtime_endpoint = data.get("realtime_endpoint") or {}
        bot = data.get("bot") or {}
        session_id = (realtime_endpoint.get("metadata") or {}).get("session_id") or (
            bot.get("metadata") or {}
        ).get("session_id")
        if session_id:
            session = self.get_session(str(session_id))
            if session is not None:
                return session
        bot_id = bot.get("id")
        if bot_id:
            return self.get_session_by_recall_bot_id(str(bot_id))
        return None

    async def _best_effort_leave_recall_bot(self, bot_id: str) -> None:
        try:
            await self._recall_client().leave_call(bot_id)
        except Exception:
            pass

    async def _best_effort_stop_gateway_session(
        self, session: Session, *, reason: str
    ) -> None:
        if session.gateway_stopped:
            return
        try:
            logger.info(
                "stopping gateway from recall webhook session_id=%s mirako_session_id=%s reason=%s",
                session.session_id,
                session.mirako_session_id,
                reason,
            )
            await self._stop_gateway_session(session)
        except Exception:
            logger.exception(
                "best-effort gateway stop failed session_id=%s mirako_session_id=%s reason=%s",
                session.session_id,
                session.mirako_session_id,
                reason,
            )

    async def _best_effort_end_created_meeting(
        self, provider: str, created_meeting: dict[str, Any] | None
    ) -> None:
        meeting_id = (created_meeting or {}).get("id")
        if not meeting_id:
            return
        try:
            await self._meeting_strategy(provider).end_meeting(meeting_id)
        except Exception:
            pass

    def _recall_client(self) -> RecallClient:
        return RecallClient(
            api_key=self.settings.recall_api_key, base_url=self.settings.recall_base_url
        )

    def _meeting_strategy(self, provider: str):
        return get_meeting_strategy(provider, self.settings)

    @staticmethod
    def _http_error_detail(exc: Exception, *, service: str) -> dict[str, Any]:
        response = getattr(exc, "response", None)
        detail: dict[str, Any] = {
            "error": f"{service}_request_failed",
            "message": str(exc),
        }
        if response is not None:
            detail["status_code"] = getattr(response, "status_code", None)
            detail["response"] = getattr(response, "text", "")
        return detail


session_service = SessionService(settings)


def _b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
