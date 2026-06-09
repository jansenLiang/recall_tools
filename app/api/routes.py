from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.responses import FileResponse

from app.core.config import settings
from app.core.paths import STATIC_DIR
from app.schemas.sessions import BridgeTelemetryRequest, CloseSessionResponse, CreateSessionRequest, CreateSessionResponse, MeetingRecordsResponse
from app.services.recall_store import recall_store
from app.services.session_service import SessionServiceError, session_service


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
        raise HTTPException(status_code=500, detail="RECALL_WEBHOOK_SECRET must start with whsec_.")
    msg_id = headers.get("webhook-id") or headers.get("svix-id")
    msg_timestamp = headers.get("webhook-timestamp") or headers.get("svix-timestamp")
    msg_signature = headers.get("webhook-signature") or headers.get("svix-signature")
    if not msg_id or not msg_timestamp or not msg_signature:
        recall_webhook_logger.warning(
            "recall webhook missing signature headers headers=%s body=%s",
            json.dumps(_safe_headers(headers), ensure_ascii=True, sort_keys=True),
            _safe_body_preview(raw_body),
        )
        raise HTTPException(status_code=400, detail="Missing Recall webhook signature headers.")

    try:
        key = base64.b64decode(secret.removeprefix("whsec_"))
    except ValueError:
        raise HTTPException(status_code=500, detail="RECALL_WEBHOOK_SECRET is not valid base64.")
    signed = b".".join([msg_id.encode("utf-8"), msg_timestamp.encode("utf-8"), raw_body])
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
        "create_session request mirako_session_id=%s meeting_provider=%s mode=%s meeting_url_provided=%s",
        req.mirako_session_id,
        req.meeting_provider,
        req.mode,
        bool(req.meeting_url),
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
        logger.error("close_session failed session_id=%s status_code=%s detail=%s", session_id, exc.status_code, exc.detail)
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


@router.post("/api/bridge-telemetry", include_in_schema=False)
async def bridge_telemetry(req: BridgeTelemetryRequest) -> dict[str, bool]:
    session = session_service.get_session(req.session_id)
    known_session = session is not None
    bridge_logger.info(
        "event=%s session_id=%s known_session=%s gateway_session_id=%s mirako_session_id=%s mode=%s elapsed_ms=%s payload=%s",
        req.event,
        req.session_id,
        known_session,
        req.gateway_session_id,
        req.mirako_session_id,
        req.mode,
        req.elapsed_ms,
        json.dumps(req.payload or {}, ensure_ascii=True, sort_keys=True),
    )
    return {"ok": True}


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
    metadata = (((payload.get("data") or {}).get("realtime_endpoint") or {}).get("metadata") or {})
    recall_webhook_logger.info(
        "recall transcript payload event=%s session_id=%s mirako_session_id=%s",
        payload.get("event"),
        metadata.get("session_id"),
        metadata.get("mirako_session_id"),
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
    metadata = (((payload.get("data") or {}).get("realtime_endpoint") or {}).get("metadata") or {})
    recall_webhook_logger.info(
        "recall participant payload event=%s session_id=%s mirako_session_id=%s",
        payload.get("event"),
        metadata.get("session_id"),
        metadata.get("mirako_session_id"),
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
    bot = ((payload.get("data") or {}).get("bot") or {})
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


@router.get("/bridge/{session_id}", response_class=HTMLResponse, include_in_schema=False)
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
    }
    return HTMLResponse(html.replace("__BRIDGE_CONFIG__", json.dumps(bridge_config)))


@router.get("/static/dd.mp3", include_in_schema=False)
async def dd_audio() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "dd.mp3",
        media_type="audio/mpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
