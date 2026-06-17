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
        metadata: dict[str, Any] | None = None,
        recording_config: dict[str, Any] | None = None,
        automatic_leave: dict[str, Any] | None = None,
        zoom: dict[str, Any] | None = None,
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
        if metadata is not None:
            payload["metadata"] = metadata
        if recording_config is not None:
            payload["recording_config"] = recording_config
        if automatic_leave is not None:
            payload["automatic_leave"] = automatic_leave
        if zoom is not None:
            payload["zoom"] = zoom
        transcript_provider = (
            ((recording_config or {}).get("transcript") or {}).get("provider") or {}
        ).get("recallai_streaming") or {}
        realtime_endpoints = (recording_config or {}).get("realtime_endpoints") or []
        endpoint_events = [endpoint.get("events") for endpoint in realtime_endpoints]
        logger.info(
            "recall create_bot payload summary output_media=%s recording_config=%s transcript_mode=%s transcript_language_code=%s realtime_endpoint_events=%s automatic_leave=%s zoom_signed_in=%s",
            output_media_url is not None,
            recording_config is not None,
            transcript_provider.get("mode"),
            transcript_provider.get("language_code"),
            endpoint_events,
            automatic_leave is not None,
            bool(zoom and zoom.get("zak_url")),
        )

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
                logger.error(
                    "recall create_bot failed status_code=%s response=%s",
                    response.status_code,
                    response.text,
                )
            response.raise_for_status()
            data = response.json()
            logger.info(
                "recall create_bot success bot_id=%s",
                data.get("id") or data.get("bot_id"),
            )
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
                logger.error(
                    "recall leave_call failed bot_id=%s status_code=%s response=%s",
                    bot_id,
                    response.status_code,
                    response.text,
                )
            response.raise_for_status()
            if not response.content:
                logger.info(
                    "recall leave_call success bot_id=%s empty_response=true", bot_id
                )
                return None
            data = response.json()
            logger.info("recall leave_call success bot_id=%s", bot_id)
            return data

    async def send_chat_message(
        self, bot_id: str, *, message: str, to: str = "everyone"
    ) -> dict[str, Any] | None:
        logger.info("recall send_chat_message start bot_id=%s to=%s", bot_id, to)
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.base_url}/bot/{bot_id}/send_chat_message/",
                headers={
                    "Authorization": self.api_key,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json={"to": to, "message": message},
            )
            if response.status_code >= 400:
                logger.error(
                    "recall send_chat_message failed bot_id=%s status_code=%s response=%s",
                    bot_id,
                    response.status_code,
                    response.text,
                )
            response.raise_for_status()
            if not response.content:
                logger.info("recall send_chat_message success bot_id=%s empty_response=true", bot_id)
                return None
            data = response.json()
            logger.info("recall send_chat_message success bot_id=%s", bot_id)
            return data
