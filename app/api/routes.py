from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse
from fastapi.responses import FileResponse
from fastapi.responses import PlainTextResponse

from app.core.config import settings
from app.core.paths import STATIC_DIR
from app.schemas.sessions import (
    BridgeTelemetryRequest,
    CaptureScreenRequest,
    CaptureScreenResponse,
    CloseSessionResponse,
    CreateSessionRequest,
    CreateSessionResponse,
    ListParticipantsResponse,
    MeetingRecordsResponse,
)
from app.services.recall_store import recall_store
from app.services.session_service import SessionServiceError, session_service
from app.services.zoom_zak import ZoomZakError, zoom_zak_service


router = APIRouter()
logger = logging.getLogger(__name__)
bridge_logger = logging.getLogger("bridge.telemetry")
recall_webhook_logger = logging.getLogger("recall.webhook")


def _safe_body_preview(raw_body: bytes, limit: int = 4096) -> str:
    text = raw_body[:limit].decode("utf-8", errors="replace")
    if len(raw_body) > limit:
        return f"{text}...<truncated {len(raw_body) - limit} bytes>"
    return text


def _safe_headers(headers: dict[str, str]) -> dict[str, str]:
    safe: dict[str, str] = {}
    for key, value in headers.items():
        lower = key.lower()
        if lower in {"authorization", "cookie", "webhook-signature", "svix-signature"}:
            safe[key] = "<redacted>"
        else:
            safe[key] = value
    return safe


def _word_text(words: object, index: int) -> str | None:
    if not isinstance(words, list) or not words:
        return None
    try:
        word = words[index]
    except IndexError:
        return None
    if not isinstance(word, dict):
        return None
    text = str(word.get("text") or "").strip()
    return text or None


def verify_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if not settings.service_api_key:
        return
    if x_api_key and secrets.compare_digest(x_api_key, settings.service_api_key):
        return
    raise HTTPException(status_code=401, detail="Invalid or missing API key.")


def verify_recall_webhook(headers: dict[str, str], raw_body: bytes) -> None:
    if not settings.recall_webhook_secret:
        return
    secret = settings.recall_webhook_secret
    if not secret.startswith("whsec_"):
        raise HTTPException(
            status_code=500, detail="RECALL_WEBHOOK_SECRET must start with whsec_."
        )
    msg_id = headers.get("webhook-id") or headers.get("svix-id")
    msg_timestamp = headers.get("webhook-timestamp") or headers.get("svix-timestamp")
    msg_signature = headers.get("webhook-signature") or headers.get("svix-signature")
    if not msg_id or not msg_timestamp or not msg_signature:
        recall_webhook_logger.warning(
            "recall webhook missing signature headers headers=%s body=%s",
            json.dumps(_safe_headers(headers), ensure_ascii=True, sort_keys=True),
            _safe_body_preview(raw_body),
        )
        raise HTTPException(
            status_code=400, detail="Missing Recall webhook signature headers."
        )

    try:
        key = base64.b64decode(secret.removeprefix("whsec_"))
    except ValueError:
        raise HTTPException(
            status_code=500, detail="RECALL_WEBHOOK_SECRET is not valid base64."
        )
    signed = b".".join(
        [msg_id.encode("utf-8"), msg_timestamp.encode("utf-8"), raw_body]
    )
    expected = hmac.new(key, signed, hashlib.sha256).digest()
    expected_base64 = base64.b64encode(expected).decode("ascii")
    # Recall uses Svix-style signatures; tolerate either space or comma separated
    # versioned tokens so we can handle the header format exposed by proxies.
    versioned_sigs = re.findall(r"v\d+,[^\s,]+", msg_signature)
    if not versioned_sigs and msg_signature:
        versioned_sigs = [msg_signature]
    for versioned_sig in versioned_sigs:
        version, _, signature = versioned_sig.partition(",")
        if version != "v1" or not signature:
            continue
        if hmac.compare_digest(expected_base64, signature):
            return
    recall_webhook_logger.warning(
        "recall webhook invalid signature headers=%s body=%s expected_len=%s signature_tokens=%s",
        json.dumps(_safe_headers(headers), ensure_ascii=True, sort_keys=True),
        _safe_body_preview(raw_body),
        len(expected_base64),
        len(versioned_sigs),
    )
    raise HTTPException(status_code=400, detail="Invalid Recall webhook signature.")


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def home() -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><title>Mirako Recall Tools</title>"
        "<h1>Mirako Recall Tools</h1>"
        "<p>Use POST /api/sessions to start a Recall bot and POST /api/sessions/{session_id}/close to stop it.</p>"
    )


@router.post(
    "/api/sessions",
    response_model=CreateSessionResponse,
    status_code=201,
    summary="Start session",
    description="Start a Recall.ai bot for a meeting and connect it to the Mirako live-stream gateway.",
)
async def create_session(
    req: CreateSessionRequest,
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> CreateSessionResponse:
    verify_api_key(x_api_key)
    logger.info(
        "create_session request mirako_session_id=%s meeting_provider=%s mode=%s meeting_url_provided=%s active_sessions=%s",
        req.mirako_session_id,
        req.meeting_provider,
        req.mode,
        bool(req.meeting_url),
        len(session_service.sessions),
    )
    try:
        response = await session_service.create_session(req)
        logger.info(
            "create_session success session_id=%s recall_bot_id=%s meeting_provider=%s created_meeting=%s",
            response.session_id,
            response.recall_bot_id,
            response.meeting_provider,
            bool(response.created_meeting),
        )
        return response
    except SessionServiceError as exc:
        logger.error(
            "create_session failed status_code=%s detail=%s mirako_session_id=%s meeting_provider=%s mode=%s meeting_url_provided=%s",
            exc.status_code,
            exc.detail,
            req.mirako_session_id,
            req.meeting_provider,
            req.mode,
            bool(req.meeting_url),
        )
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.post(
    "/api/sessions/{session_id}/close",
    response_model=CloseSessionResponse,
    summary="Close session",
    description="Stop the Recall.ai bot, stop the live-stream-gateway session, and end a service-created meeting when applicable.",
)
async def close_session(
    session_id: str,
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> CloseSessionResponse:
    verify_api_key(x_api_key)
    logger.info("close_session request session_id=%s", session_id)
    try:
        response = await session_service.close_session(session_id)
        logger.info(
            "close_session success session_id=%s recall_left=%s gateway_stopped=%s zoom_ended=%s",
            response.session_id,
            response.recall_left,
            response.gateway_stopped,
            response.zoom_ended,
        )
        return response
    except SessionServiceError as exc:
        logger.error(
            "close_session failed session_id=%s status_code=%s detail=%s",
            session_id,
            exc.status_code,
            exc.detail,
        )
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.get(
    "/api/sessions/{session_id}/participants",
    response_model=ListParticipantsResponse,
    summary="List meeting participants",
    description="Return the participants currently known to recall_tools, joined with the cached H.264 frame status and the most recent screenshare sender.",
)
async def list_participants(
    session_id: str,
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> ListParticipantsResponse:
    verify_api_key(x_api_key)
    try:
        return await session_service.list_participants(session_id)
    except SessionServiceError as exc:
        logger.warning(
            "list_participants failed session_id=%s status_code=%s detail=%s",
            session_id,
            exc.status_code,
            exc.detail,
        )
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.post(
    "/api/sessions/{session_id}/capture",
    response_model=CaptureScreenResponse,
    summary="Capture current meeting screen",
    description="Decode the latest cached H.264 IDR frame for the requested participant (or the most recent screenshare sender) and optionally describe it with the MiniMax multimodal LLM.",
)
async def capture_screen(
    session_id: str,
    req: CaptureScreenRequest,
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> CaptureScreenResponse:
    verify_api_key(x_api_key)
    try:
        return await session_service.capture_screen(session_id, req)
    except SessionServiceError as exc:
        logger.warning(
            "capture_screen failed session_id=%s status_code=%s detail=%s",
            session_id,
            exc.status_code,
            exc.detail,
        )
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.get(
    "/api/meeting-records",
    response_model=MeetingRecordsResponse,
    summary="Get meeting records",
    description="Get transcript meeting records captured from Recall.ai webhooks. Supports optional pagination; without limit it returns all matching records.",
)
async def get_meeting_records(
    session_id: str | None = Query(default=None, min_length=1),
    mirako_session_id: str | None = Query(default=None, min_length=1),
    limit: int | None = Query(default=None, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> MeetingRecordsResponse:
    verify_api_key(x_api_key)
    data = recall_store.get_meeting_records(
        session_id=session_id,
        mirako_session_id=mirako_session_id,
        limit=limit,
        offset=offset,
    )
    return MeetingRecordsResponse(**data)


@router.get("/api/zoom/zak", response_class=PlainTextResponse, include_in_schema=False)
async def zoom_zak_callback(secret: str = Query(default="")) -> PlainTextResponse:
    expected = settings.zoom_zak_callback_secret
    if not expected or not secret or not secrets.compare_digest(secret, expected):
        logger.warning("zoom zak callback rejected invalid secret")
        raise HTTPException(status_code=401, detail="Invalid ZAK callback secret.")
    try:
        token = await zoom_zak_service.get_zak()
    except ZoomZakError as exc:
        logger.error("zoom zak callback failed error=%s", exc)
        raise HTTPException(status_code=502, detail=str(exc))
    return PlainTextResponse(token)


@router.post("/api/bridge-telemetry", include_in_schema=False)
async def bridge_telemetry(req: BridgeTelemetryRequest) -> dict[str, bool]:
    session = session_service.get_session(req.session_id)
    known_session = session is not None
    session_closed = bool(session and session.closed)
    bridge_logger.info(
        "event=%s session_id=%s known_session=%s session_closed=%s closed_reason=%s gateway_session_id=%s mirako_session_id=%s mode=%s elapsed_ms=%s payload=%s",
        req.event,
        req.session_id,
        known_session,
        session_closed,
        session.closed_reason if session else None,
        req.gateway_session_id,
        req.mirako_session_id,
        req.mode,
        req.elapsed_ms,
        json.dumps(req.payload or {}, ensure_ascii=True, sort_keys=True),
    )
    return {"ok": True}


@router.get("/api/sessions/{session_id}/bridge-status", include_in_schema=False)
async def bridge_session_status(session_id: str) -> dict[str, object]:
    status = session_service.get_bridge_status(session_id)
    if status is None:
        logger.warning(
            "bridge status requested for unknown session_id=%s; gateway reconnect allowed because no terminal close mark exists",
            session_id,
        )
        return {
            "session_id": session_id,
            "closed": False,
            "closed_reason": "unknown_session",
            "can_connect_gateway": True,
        }
    logger.info(
        "bridge status result session_id=%s mirako_session_id=%s closed=%s closed_reason=%s gateway_stopped=%s can_connect_gateway=%s",
        session_id,
        status.get("mirako_session_id"),
        status.get("closed"),
        status.get("closed_reason"),
        status.get("gateway_stopped"),
        status.get("can_connect_gateway"),
    )
    if not status["can_connect_gateway"]:
        logger.info(
            "bridge status blocks gateway connect session_id=%s mirako_session_id=%s closed_reason=%s gateway_stopped=%s",
            session_id,
            status.get("mirako_session_id"),
            status.get("closed_reason"),
            status.get("gateway_stopped"),
        )
    return status


@router.post("/api/recall/transcript", include_in_schema=False)
async def recall_transcript_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, bool]:
    raw_body = await request.body()
    headers = {key.lower(): value for key, value in request.headers.items()}
    verify_recall_webhook(headers, raw_body)
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except ValueError:
        recall_webhook_logger.warning(
            "recall transcript invalid json headers=%s body=%s",
            json.dumps(_safe_headers(headers), ensure_ascii=True, sort_keys=True),
            _safe_body_preview(raw_body),
        )
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")
    metadata = ((payload.get("data") or {}).get("realtime_endpoint") or {}).get(
        "metadata"
    ) or {}
    transcript_data = (payload.get("data") or {}).get("data") or {}
    words = transcript_data.get("words") or []
    recall_webhook_logger.info(
        "recall transcript payload event=%s session_id=%s mirako_session_id=%s language_code=%s words=%s first_word=%s last_word=%s",
        payload.get("event"),
        metadata.get("session_id"),
        metadata.get("mirako_session_id"),
        transcript_data.get("language_code"),
        len(words) if isinstance(words, list) else 0,
        _word_text(words, 0),
        _word_text(words, -1),
    )
    background_tasks.add_task(session_service.handle_recall_transcript, payload)
    return {"ok": True}


@router.post("/api/recall/participant-events", include_in_schema=False)
async def recall_participant_events_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, bool]:
    raw_body = await request.body()
    headers = {key.lower(): value for key, value in request.headers.items()}
    verify_recall_webhook(headers, raw_body)
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except ValueError:
        recall_webhook_logger.warning(
            "recall participant invalid json headers=%s body=%s",
            json.dumps(_safe_headers(headers), ensure_ascii=True, sort_keys=True),
            _safe_body_preview(raw_body),
        )
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")
    metadata = ((payload.get("data") or {}).get("realtime_endpoint") or {}).get(
        "metadata"
    ) or {}
    participant_event_data = (payload.get("data") or {}).get("data") or {}
    chat_data = participant_event_data.get("data") or {}
    participant = participant_event_data.get("participant") or {}
    recall_webhook_logger.info(
        "recall participant payload event=%s session_id=%s mirako_session_id=%s participant_id=%s participant_name=%s chat_to=%s chat_text_preview=%s",
        payload.get("event"),
        metadata.get("session_id"),
        metadata.get("mirako_session_id"),
        participant.get("id") if isinstance(participant, dict) else None,
        participant.get("name") if isinstance(participant, dict) else None,
        chat_data.get("to") if isinstance(chat_data, dict) else None,
        str(chat_data.get("text") or "")[:200] if isinstance(chat_data, dict) else None,
    )
    background_tasks.add_task(session_service.handle_recall_participant_event, payload)
    return {"ok": True}


@router.post("/api/recall/bot-status", include_in_schema=False)
async def recall_bot_status_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, bool]:
    raw_body = await request.body()
    headers = {key.lower(): value for key, value in request.headers.items()}
    verify_recall_webhook(headers, raw_body)
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except ValueError:
        recall_webhook_logger.warning(
            "recall bot status invalid json headers=%s body=%s",
            json.dumps(_safe_headers(headers), ensure_ascii=True, sort_keys=True),
            _safe_body_preview(raw_body),
        )
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")
    bot = (payload.get("data") or {}).get("bot") or {}
    metadata = bot.get("metadata") or {}
    recall_webhook_logger.info(
        "recall bot status payload event=%s session_id=%s mirako_session_id=%s bot_id=%s",
        payload.get("event"),
        metadata.get("session_id"),
        metadata.get("mirako_session_id"),
        bot.get("id"),
    )
    background_tasks.add_task(session_service.handle_recall_bot_status, payload)
    return {"ok": True}


@router.websocket("/api/recall/realtime/{session_id}")
async def recall_realtime_websocket(
    websocket: WebSocket,
    session_id: str,
    token: str | None = Query(default=None),
) -> None:
    if settings.service_api_key and not (
        token and secrets.compare_digest(token, settings.service_api_key)
    ):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    logger.info("recall realtime websocket accepted session_id=%s", session_id)
    messages = 0
    h264_events = 0
    try:
        while True:
            raw = await websocket.receive_text()
            messages += 1
            try:
                payload = json.loads(raw)
            except ValueError:
                logger.warning(
                    "recall realtime websocket invalid json session_id=%s message=%s",
                    session_id,
                    raw[:500],
                )
                continue
            try:
                handled = await session_service.handle_recall_realtime_payload(
                    session_id, payload
                )
            except Exception:
                logger.exception(
                    "recall realtime websocket payload failed session_id=%s event=%s",
                    session_id,
                    payload.get("event") if isinstance(payload, dict) else None,
                )
                continue
            if handled:
                h264_events += 1
    except WebSocketDisconnect:
        logger.info(
            "recall realtime websocket disconnected session_id=%s messages=%s h264_events=%s",
            session_id,
            messages,
            h264_events,
        )


@router.get(
    "/bridge/{session_id}", response_class=HTMLResponse, include_in_schema=False
)
async def bridge(session_id: str) -> HTMLResponse:
    session = session_service.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown or expired session.")
    html = (STATIC_DIR / "bridge.html").read_text(encoding="utf-8")
    bridge_config = {
        "sessionId": session.session_id,
        "createdAt": int(session.created_at),
        "gatewayUrl": session.gateway_url,
        "mirakoSessionId": session.mirako_session_id,
        "mode": session.mode,
        "conversationMode": session.conversation_mode,
        "closed": session.closed,
        "closedReason": session.closed_reason,
    }
    return HTMLResponse(html.replace("__BRIDGE_CONFIG__", json.dumps(bridge_config)))


@router.get("/static/dd.mp3", include_in_schema=False)
async def dd_audio() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "dd.mp3",
        media_type="audio/mpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
