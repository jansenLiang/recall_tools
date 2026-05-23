from __future__ import annotations

import base64
import logging
from typing import Any

import httpx


class ZoomMeetingError(RuntimeError):
    pass


logger = logging.getLogger(__name__)


class ZoomMeetingClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        account_id: str,
    ) -> None:
        if not client_id:
            raise ZoomMeetingError("ZOOM_OAUTH_CLIENT_ID is required.")
        if not client_secret:
            raise ZoomMeetingError("ZOOM_OAUTH_CLIENT_SECRET is required.")
        if not account_id:
            raise ZoomMeetingError("ZOOM_OAUTH_ACCOUNT_ID is required.")
        self.client_id = client_id
        self.client_secret = client_secret
        self.account_id = account_id

    async def create_meeting(self, meeting: dict[str, Any], *, user_id: str) -> dict[str, Any]:
        logger.info("zoom create_meeting start user_id=%s topic=%s", user_id or "me", meeting.get("topic"))
        token = await self._access_token()
        safe_user_id = user_id or "me"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"https://api.zoom.us/v2/users/{safe_user_id}/meetings",
                headers={"Authorization": f"Bearer {token}"},
                json=meeting,
            )
        if response.status_code >= 400:
            logger.error("zoom create_meeting failed status_code=%s response=%s", response.status_code, response.text)
            raise ZoomMeetingError(f"Zoom create meeting failed: HTTP {response.status_code} {response.text}")
        logger.info("zoom create_meeting success status_code=%s", response.status_code)
        return response.json()

    async def end_meeting(self, meeting_id: str | int) -> dict[str, Any] | None:
        logger.info("zoom end_meeting start meeting_id=%s", meeting_id)
        token = await self._access_token()
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.put(
                f"https://api.zoom.us/v2/meetings/{meeting_id}/status",
                headers={"Authorization": f"Bearer {token}"},
                json={"action": "end"},
            )
        if response.status_code >= 400:
            logger.error("zoom end_meeting failed meeting_id=%s status_code=%s response=%s", meeting_id, response.status_code, response.text)
            raise ZoomMeetingError(f"Zoom end meeting failed: HTTP {response.status_code} {response.text}")
        logger.info("zoom end_meeting success meeting_id=%s status_code=%s", meeting_id, response.status_code)
        if not response.content:
            return None
        return response.json()

    async def _access_token(self) -> str:
        logger.info("zoom oauth token request start account_id_present=%s", bool(self.account_id))
        tokens = await self._token_request(
            {"grant_type": "account_credentials", "account_id": self.account_id}
        )
        token = tokens.get("access_token")
        if not isinstance(token, str) or not token:
            logger.error("zoom oauth token response missing access_token keys=%s", sorted(tokens.keys()))
            raise ZoomMeetingError(f"Zoom OAuth token response has no access_token: {tokens}")
        logger.info("zoom oauth token request success")
        return token

    async def _token_request(self, form: dict[str, str]) -> dict[str, Any]:
        raw = f"{self.client_id}:{self.client_secret}".encode("utf-8")
        auth = "Basic " + base64.b64encode(raw).decode("ascii")
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://zoom.us/oauth/token",
                headers={"Authorization": auth},
                data=form,
            )
        if response.status_code >= 400:
            logger.error("zoom oauth token request failed status_code=%s response=%s", response.status_code, response.text)
            raise ZoomMeetingError(f"Zoom OAuth token request failed: HTTP {response.status_code} {response.text}")
        return response.json()
