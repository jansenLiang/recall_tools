from __future__ import annotations

import logging
from typing import Any

import httpx


logger = logging.getLogger(__name__)


class RecallClient:
    def __init__(self, *, api_key: str, base_url: str) -> None:
        if not api_key:
            raise ValueError("RECALL_API_KEY is required.")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    async def create_bot(
        self,
        *,
        meeting_url: str,
        bot_name: str,
        variant: str,
        output_media_url: str | None = None,
    ) -> dict[str, Any]:
        logger.info(
            "recall create_bot start meeting_url_provided=%s output_media=%s variant=%s",
            bool(meeting_url),
            output_media_url is not None,
            variant,
        )
        payload: dict[str, Any] = {
            "meeting_url": meeting_url,
            "bot_name": bot_name,
            "variant": {
                "zoom": variant,
                "google_meet": variant,
                "microsoft_teams": variant,
            },
        }
        if output_media_url is not None:
            payload["output_media"] = self._webpage_output_media(output_media_url)

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.base_url}/bot/",
                headers={
                    "Authorization": self.api_key,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if response.status_code >= 400:
                logger.error("recall create_bot failed status_code=%s response=%s", response.status_code, response.text)
            response.raise_for_status()
            data = response.json()
            logger.info("recall create_bot success bot_id=%s", data.get("id") or data.get("bot_id"))
            return data

    @staticmethod
    def _webpage_output_media(url: str) -> dict[str, Any]:
        return {
            "camera": {
                "kind": "webpage",
                "config": {"url": url},
            }
        }

    async def leave_call(self, bot_id: str) -> dict[str, Any] | None:
        logger.info("recall leave_call start bot_id=%s", bot_id)
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.base_url}/bot/{bot_id}/leave_call/",
                headers={
                    "Authorization": self.api_key,
                    "Accept": "application/json",
                },
            )
            if response.status_code >= 400:
                logger.error("recall leave_call failed bot_id=%s status_code=%s response=%s", bot_id, response.status_code, response.text)
            response.raise_for_status()
            if not response.content:
                logger.info("recall leave_call success bot_id=%s empty_response=true", bot_id)
                return None
            data = response.json()
            logger.info("recall leave_call success bot_id=%s", bot_id)
            return data
