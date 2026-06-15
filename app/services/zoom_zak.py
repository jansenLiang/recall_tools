from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any

import httpx

from app.core.config import Settings, settings


logger = logging.getLogger(__name__)


class ZoomZakError(RuntimeError):
    pass


class ZoomZakService:
    def __init__(self, app_settings: Settings) -> None:
        self.settings = app_settings
        self._lock = asyncio.Lock()
        self._access_token = ""
        self._access_token_expires_at = 0.0
        self._zak_token = ""
        self._zak_expires_at = 0.0

    async def get_zak(self) -> str:
        if not self.settings.zoom_signed_in_enabled:
            raise ZoomZakError("ZOOM_SIGNED_IN_ENABLED is false.")
        self._validate_config()
        async with self._lock:
            now = time.time()
            if self._zak_token and now < self._zak_expires_at:
                return self._zak_token
            if not self._access_token or now >= self._access_token_expires_at:
                await self._refresh_access_token()
            await self._refresh_zak_token()
            return self._zak_token

    def clear_cache(self) -> None:
        self._access_token = ""
        self._access_token_expires_at = 0.0
        self._zak_token = ""
        self._zak_expires_at = 0.0

    def _validate_config(self) -> None:
        missing = [
            name
            for name, value in {
                "ZOOM_OAUTH_CLIENT_ID": self.settings.zoom_oauth_client_id,
                "ZOOM_OAUTH_CLIENT_SECRET": self.settings.zoom_oauth_client_secret,
                "ZOOM_OAUTH_ACCOUNT_ID": self.settings.zoom_oauth_account_id,
            }.items()
            if not value
        ]
        if missing:
            raise ZoomZakError(f"Missing Zoom OAuth config: {', '.join(missing)}")

    async def _refresh_access_token(self) -> None:
        logger.info("zoom zak oauth token request start")
        raw = f"{self.settings.zoom_oauth_client_id}:{self.settings.zoom_oauth_client_secret}".encode(
            "utf-8"
        )
        auth = "Basic " + base64.b64encode(raw).decode("ascii")
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://zoom.us/oauth/token",
                headers={"Authorization": auth},
                data={
                    "grant_type": "account_credentials",
                    "account_id": self.settings.zoom_oauth_account_id,
                },
            )
        data = self._json_or_text(response)
        if response.status_code >= 400:
            logger.error(
                "zoom zak oauth token request failed status_code=%s response=%s",
                response.status_code,
                data,
            )
            raise ZoomZakError(
                f"Zoom OAuth token request failed: HTTP {response.status_code} {data}"
            )
        token = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise ZoomZakError(f"Zoom OAuth token response has no access_token: {data}")
        expires_in = int(data.get("expires_in") or 3600) if isinstance(data, dict) else 3600
        self._access_token = token
        self._access_token_expires_at = time.time() + max(60, expires_in - 300)
        logger.info("zoom zak oauth token request success expires_in=%s", expires_in)

    async def _refresh_zak_token(self) -> None:
        user_id = self.settings.zoom_zak_user_id or "me"
        logger.info("zoom zak token request start user_id=%s", user_id)
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"https://api.zoom.us/v2/users/{user_id}/token",
                params={"type": "zak"},
                headers={"Authorization": f"Bearer {self._access_token}"},
            )
        data = self._json_or_text(response)
        if response.status_code in {401, 403}:
            self.clear_cache()
        if response.status_code >= 400:
            logger.error(
                "zoom zak token request failed user_id=%s status_code=%s response=%s",
                user_id,
                response.status_code,
                data,
            )
            raise ZoomZakError(
                f"Zoom ZAK token request failed: HTTP {response.status_code} {data}"
            )
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise ZoomZakError(f"Zoom ZAK response has no token: {data}")
        ttl = max(60, self.settings.zoom_zak_cache_ttl_seconds)
        self._zak_token = token
        self._zak_expires_at = time.time() + ttl
        logger.info("zoom zak token request success user_id=%s cache_ttl=%s", user_id, ttl)

    @staticmethod
    def _json_or_text(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return response.text


zoom_zak_service = ZoomZakService(settings)
