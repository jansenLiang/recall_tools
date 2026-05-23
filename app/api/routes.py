from __future__ import annotations

import json
import logging
import secrets

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse

from app.core.config import settings
from app.core.paths import STATIC_DIR
from app.schemas.sessions import CloseSessionResponse, CreateSessionRequest, CreateSessionResponse
from app.services.session_service import SessionServiceError, session_service


router = APIRouter()
logger = logging.getLogger(__name__)


def verify_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if not settings.service_api_key:
        return
    if x_api_key and secrets.compare_digest(x_api_key, settings.service_api_key):
        return
    raise HTTPException(status_code=401, detail="Invalid or missing API key.")


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
    }
    return HTMLResponse(html.replace("__BRIDGE_CONFIG__", json.dumps(bridge_config)))
