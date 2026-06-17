from __future__ import annotations

import json
import logging
import time
from typing import AsyncIterator

import httpx


logger = logging.getLogger(__name__)


class HermesAgentError(Exception):
    def __init__(self, status_code: int | None, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class HermesAgentClient:
    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
    ) -> None:
        self.api_url = api_url.strip()
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.timeout_seconds = timeout_seconds

    def enabled(self) -> bool:
        return bool(self.api_url and self.api_key and self.model)

    def _headers(self, session_id: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "X-Recall-Session-Id": session_id,
        }

    async def chat(self, *, session_id: str, message: str) -> AsyncIterator[str]:
        if not self.enabled():
            raise HermesAgentError(None, "Hermes agent is not configured.")

        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": message}],
            "stream": True,
        }
        response_id = f"hermes-{int(time.time() * 1000)}"

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                async with client.stream(
                    "POST",
                    self.api_url,
                    headers=self._headers(session_id),
                    json=payload,
                ) as response:
                    if response.status_code >= 400:
                        body = await response.aread()
                        detail = body.decode("utf-8", errors="replace")
                        logger.error(
                            "hermes agent request failed session_id=%s status_code=%s response=%s",
                            session_id,
                            response.status_code,
                            detail[:1000],
                        )
                        raise HermesAgentError(response.status_code, detail)

                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data_text = line.removeprefix("data:").strip()
                        if data_text == "[DONE]":
                            break
                        try:
                            data = json.loads(data_text)
                            choice = data["choices"][0]
                        except (KeyError, IndexError, TypeError, ValueError) as exc:
                            raise HermesAgentError(
                                None, "Hermes agent stream response missing choices[0]."
                            ) from exc
                        response_id = str(data.get("id") or response_id)
                        delta = choice.get("delta") or {}
                        content = delta.get("content")
                        if content:
                            yield str(content)
        except HermesAgentError:
            raise
        except httpx.HTTPError as exc:
            raise HermesAgentError(None, str(exc)) from exc
