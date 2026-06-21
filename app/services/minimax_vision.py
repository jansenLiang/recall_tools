from __future__ import annotations

import base64
import io
import logging
from typing import Any

import httpx


logger = logging.getLogger(__name__)


class MinimaxVisionError(RuntimeError):
    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


class MinimaxVisionClient:
    def __init__(
        self,
        *,
        api_base: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 60.0,
    ) -> None:
        if not api_key:
            raise MinimaxVisionError(500, "MINIMAX_API_KEY is required.")
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = max(5.0, float(timeout_seconds))

    async def describe_image(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        prompt: str,
    ) -> str:
        if not image_bytes:
            raise MinimaxVisionError(400, "image_bytes is empty.")
        encoded = base64.b64encode(image_bytes).decode("ascii")
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": encoded,
                            },
                        },
                    ],
                }
            ],
            "max_tokens": 1024,
            "thinking": {"type": "adaptive"},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        url = f"{self.api_base}/anthropic/v1/messages"
        logger.info(
            "minimax vision request model=%s mime=%s bytes=%s",
            self.model,
            mime_type,
            len(image_bytes),
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            logger.exception("minimax vision request failed")
            raise MinimaxVisionError(
                502, {"error": "minimax_request_failed", "message": str(exc)}
            ) from exc

        if response.status_code >= 400:
            logger.error(
                "minimax vision request rejected status=%s body=%s",
                response.status_code,
                response.text[:500],
            )
            raise MinimaxVisionError(
                response.status_code,
                {"error": "minimax_request_rejected", "body": response.text},
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise MinimaxVisionError(
                502, {"error": "minimax_invalid_response", "message": str(exc)}
            ) from exc

        content = data.get("content")
        if content is None:
            choices = data.get("choices") or []
            if not choices:
                raise MinimaxVisionError(
                    502, {"error": "minimax_no_content", "body": data}
                )
            message = choices[0].get("message") or {}
            content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
            joined = "\n".join(part for part in parts if part).strip()
            if joined:
                return joined
        raise MinimaxVisionError(502, {"error": "minimax_empty_content", "body": data})


def png_bytes_for_response(image_bytes: bytes) -> str:
    return base64.b64encode(image_bytes).decode("ascii")


def is_png(data: bytes) -> bool:
    return data.startswith(b"\x89PNG\r\n\x1a\n")


def normalize_mime(image_bytes: bytes, hint: str | None = None) -> str:
    if hint:
        return hint
    if is_png(image_bytes):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8"):
        return "image/jpeg"
    return "application/octet-stream"


def bytes_to_buffer(image_bytes: bytes) -> io.BytesIO:
    return io.BytesIO(image_bytes)
