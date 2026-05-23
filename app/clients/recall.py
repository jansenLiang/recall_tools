from __future__ import annotations

from typing import Any

import httpx


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
        automatic_leave: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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
        if automatic_leave is not None:
            payload["automatic_leave"] = automatic_leave
        if metadata is not None:
            payload["metadata"] = metadata

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
            response.raise_for_status()
            return response.json()

    @staticmethod
    def _webpage_output_media(url: str) -> dict[str, Any]:
        return {
            "camera": {
                "kind": "webpage",
                "config": {"url": url},
            }
        }

    async def leave_call(self, bot_id: str) -> dict[str, Any] | None:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.base_url}/bot/{bot_id}/leave_call/",
                headers={
                    "Authorization": self.api_key,
                    "Accept": "application/json",
                },
            )
            response.raise_for_status()
            if not response.content:
                return None
            return response.json()
