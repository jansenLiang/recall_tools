from __future__ import annotations

import secrets
import time
import logging
from typing import Any

import httpx

from app.clients.recall import RecallClient
from app.core.config import Settings, settings
from app.models.session import Session
from app.schemas.sessions import CloseSessionResponse, CreateSessionRequest, CreateSessionResponse
from app.services.meeting_strategies import get_meeting_strategy
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

    def get_session(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    def get_session_by_recall_bot_id(self, bot_id: str) -> Session | None:
        for session in self.sessions.values():
            if session.recall_bot_id == bot_id:
                return session
        return None

    async def create_session(self, req: CreateSessionRequest) -> CreateSessionResponse:
        mirako_session_id = req.mirako_session_id
        gateway_url = self._normalize_gateway_url(None)

        public_base_url = self._normalize_public_base_url(req.public_base_url)
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
                    {"error": f"{req.meeting_provider}_join_url_missing", req.meeting_provider: created_meeting},
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
        self.sessions[session_id] = Session(
            session_id=session_id,
            created_at=time.time(),
            mirako_session_id=mirako_session_id,
            gateway_url=gateway_url,
            mode=req.mode,
            meeting_provider=req.meeting_provider,
            meeting_url=meeting_url,
            bridge_url=bridge_url,
            recall_bot_id="",
            recall_bot={},
            transcript_utterances=[],
            meeting_participants={},
            meeting_participant_count=0,
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
            bot = await self._create_recall_bot(req, meeting_url=meeting_url, bridge_url=bridge_url)
        except SessionServiceError:
            self.sessions.pop(session_id, None)
            await self._best_effort_end_created_meeting(req.meeting_provider, created_meeting)
            logger.warning("session rolled back after recall bot creation failure session_id=%s", session_id)
            raise

        session = self.sessions[session_id]
        session.recall_bot = bot
        session.recall_bot_id = str(bot.get("id") or bot.get("bot_id") or "")
        if not session.recall_bot_id:
            self.sessions.pop(session_id, None)
            await self._best_effort_end_created_meeting(req.meeting_provider, created_meeting)
            raise SessionServiceError(502, {"error": "recall_bot_id_missing", "bot": bot})
        recall_store.upsert_session(
            session_id=session_id,
            mirako_session_id=mirako_session_id,
            recall_bot_id=session.recall_bot_id,
            recall_bot=bot,
        )
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
                logger.warning("recall transcript webhook unknown session payload=%s", payload)
                return {"ok": True, "ignored": True, "reason": "unknown_session"}
            recall_store.add_event(
                event_type=event,
                session_id=session.session_id,
                mirako_session_id=session.mirako_session_id,
                recall_bot_id=session.recall_bot_id,
                payload=payload,
            )

            transcript_data = ((payload.get("data") or {}).get("data") or {})
            words = transcript_data.get("words") or []
            content = " ".join(str(word.get("text") or "").strip() for word in words if isinstance(word, dict)).strip()
            if not content:
                return {"ok": True, "ignored": True, "reason": "empty_content"}

            participant = transcript_data.get("participant") or {}
            speaker = str(participant.get("name") or participant.get("email") or participant.get("id") or "default")
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
                "recall transcript stored session_id=%s speaker=%s words=%s chars=%s",
                session.session_id,
                speaker,
                len(words),
                len(content),
            )
            return {"ok": True, "stored": True}
        except Exception:
            logger.exception("recall transcript processing failed")
            return {"ok": False}

    async def handle_recall_participant_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            event = str(payload.get("event") or "")
            if event not in {"participant_events.join", "participant_events.leave", "participant_events.update"}:
                logger.info("recall participant webhook ignored event=%s", event)
                return {"ok": True, "ignored": True}

            session = self._session_from_recall_payload(payload)
            if session is None:
                logger.warning("recall participant webhook unknown session payload=%s", payload)
                return {"ok": True, "ignored": True, "reason": "unknown_session"}
            recall_store.add_event(
                event_type=event,
                session_id=session.session_id,
                mirako_session_id=session.mirako_session_id,
                recall_bot_id=session.recall_bot_id,
                payload=payload,
            )

            participant = (((payload.get("data") or {}).get("data") or {}).get("participant") or {})
            participant_id = self._participant_key(participant)
            if not participant_id:
                logger.warning("recall participant webhook missing participant id event=%s payload=%s", event, payload)
                return {"ok": True, "ignored": True, "reason": "missing_participant_id"}

            if event == "participant_events.leave":
                session.meeting_participants.pop(participant_id, None)
            else:
                session.meeting_participants[participant_id] = participant

            session.meeting_participant_count = len(session.meeting_participants)
            desired_mode = self._desired_conversation_mode(session.meeting_participant_count)
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

    async def close_session(self, session_id: str) -> CloseSessionResponse:
        session = self.sessions.pop(session_id, None)
        if session is None:
            raise SessionServiceError(404, "Unknown session_id.")

        recall_response = None
        recall_left = False
        if session.recall_bot_id:
            try:
                logger.info("leaving recall bot session_id=%s recall_bot_id=%s", session_id, session.recall_bot_id)
                recall_response = await self._recall_client().leave_call(session.recall_bot_id)
                recall_left = True
                recall_store.add_event(
                    event_type="bot.leave_call",
                    session_id=session.session_id,
                    mirako_session_id=session.mirako_session_id,
                    recall_bot_id=session.recall_bot_id,
                    payload=recall_response,
                )
            except Exception as exc:
                raise SessionServiceError(502, self._http_error_detail(exc, service="recall"))
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
        except Exception as exc:
            raise SessionServiceError(502, self._http_error_detail(exc, service="gateway"))

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
                    zoom_response = await self._meeting_strategy(session.meeting_provider).end_meeting(meeting_id)
                    zoom_ended = True
                except Exception as exc:
                    raise SessionServiceError(502, self._http_error_detail(exc, service=session.meeting_provider))

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
            return await self._meeting_strategy(req.meeting_provider).create_meeting(req)
        except Exception as exc:
            logger.exception("create meeting failed provider=%s", req.meeting_provider)
            raise SessionServiceError(502, self._http_error_detail(exc, service=req.meeting_provider))

    async def _create_recall_bot(self, req: CreateSessionRequest, *, meeting_url: str, bridge_url: str) -> dict[str, Any]:
        try:
            logger.info(
                "creating recall bot provider=%s mode=%s bridge_url=%s",
                req.meeting_provider,
                req.mode,
                bridge_url,
            )
            return await self._recall_client().create_bot(
                meeting_url=meeting_url,
                bot_name=req.bot_name or self.settings.bot_name,
                variant=self.settings.recall_bot_variant,
                output_media_url=bridge_url,
                metadata={"session_id": bridge_url.rsplit("/", 1)[-1], "mirako_session_id": req.mirako_session_id},
                recording_config=self._recall_recording_config(req, bridge_url),
            )
        except httpx.HTTPStatusError as exc:
            logger.exception("recall bot creation failed status_code=%s", exc.response.status_code)
            raise SessionServiceError(exc.response.status_code, self._http_error_detail(exc, service="recall"))
        except Exception as exc:
            logger.exception("recall bot creation failed")
            raise SessionServiceError(502, self._http_error_detail(exc, service="recall"))

    async def _stop_gateway_session(self, session: Session) -> dict[str, Any] | None:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{session.gateway_url}/api/sessions/{session.mirako_session_id}/stop",
                headers={"Accept": "application/json"},
            )
            if response.status_code == 404:
                logger.warning("gateway session not found mirako_session_id=%s", session.mirako_session_id)
                return {"status_code": 404, "message": "Gateway session was already stopped or not found."}
            response.raise_for_status()
            if not response.content:
                return None
            try:
                return response.json()
            except ValueError:
                return {"status_code": response.status_code, "response": response.text}

    async def _set_gateway_conversation_mode(self, session: Session, mode: str) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{session.gateway_url}/api/sessions/{session.mirako_session_id}/mode",
                headers={"Accept": "application/json", "Content-Type": "application/json"},
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

    def _recall_recording_config(self, req: CreateSessionRequest, bridge_url: str) -> dict[str, Any] | None:
        if not self.settings.recall_transcript_enabled:
            return None
        public_base_url = bridge_url.rsplit("/bridge/", 1)[0]
        session_id = bridge_url.rsplit("/", 1)[-1]
        return {
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
                    "metadata": {"session_id": session_id, "mirako_session_id": req.mirako_session_id},
                },
                {
                    "type": "webhook",
                    "url": f"{public_base_url}/api/recall/participant-events",
                    "events": [
                        "participant_events.join",
                        "participant_events.leave",
                        "participant_events.update",
                    ],
                    "metadata": {"session_id": session_id, "mirako_session_id": req.mirako_session_id},
                }
            ],
        }

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
            word_start = self._first_number(word, ("start_time", "start_timestamp", "start", "timestamp"))
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

    def _desired_conversation_mode(self, participant_count: int) -> str | None:
        policy = self.settings.conversation_mode_policy
        if policy in {"multi", "single"}:
            return None
        return "multi" if participant_count > 2 else "single"

    def _session_from_recall_payload(self, payload: dict[str, Any]) -> Session | None:
        data = payload.get("data") or {}
        realtime_endpoint = data.get("realtime_endpoint") or {}
        bot = data.get("bot") or {}
        session_id = (realtime_endpoint.get("metadata") or {}).get("session_id") or (bot.get("metadata") or {}).get("session_id")
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

    async def _best_effort_end_created_meeting(self, provider: str, created_meeting: dict[str, Any] | None) -> None:
        meeting_id = (created_meeting or {}).get("id")
        if not meeting_id:
            return
        try:
            await self._meeting_strategy(provider).end_meeting(meeting_id)
        except Exception:
            pass

    def _recall_client(self) -> RecallClient:
        return RecallClient(api_key=self.settings.recall_api_key, base_url=self.settings.recall_base_url)

    def _meeting_strategy(self, provider: str):
        return get_meeting_strategy(provider, self.settings)

    @staticmethod
    def _http_error_detail(exc: Exception, *, service: str) -> dict[str, Any]:
        response = getattr(exc, "response", None)
        detail: dict[str, Any] = {"error": f"{service}_request_failed", "message": str(exc)}
        if response is not None:
            detail["status_code"] = getattr(response, "status_code", None)
            detail["response"] = getattr(response, "text", "")
        return detail


session_service = SessionService(settings)
