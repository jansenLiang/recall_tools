from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

import websockets
from websockets.client import WebSocketClientProtocol
from websockets.exceptions import ConnectionClosed, WebSocketException


logger = logging.getLogger(__name__)


H264_EVENT = "video_separate_h264.data"


class RecallRealtimeClient:
    """Manages a single Recall.ai realtime WebSocket subscription per session.

    Receives `video_separate_h264.data` events and forwards the base64 H.264
    NAL buffer to a callback for frame caching. Reconnects with exponential
    backoff on connection drops.
    """

    def __init__(
        self,
        *,
        ws_url: str,
        api_key: str,
        bot_id: str,
        events: list[str],
        on_event: Callable[[str, dict[str, Any]], asyncio.Future[None] | None],
        ping_interval: float = 20.0,
        ping_timeout: float = 20.0,
        max_backoff: float = 30.0,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required for RecallRealtimeClient.")
        if not bot_id:
            raise ValueError("bot_id is required for RecallRealtimeClient.")
        self.ws_url = ws_url.rstrip("/")
        self.api_key = api_key
        self.bot_id = bot_id
        self.events = list(events) or [H264_EVENT]
        self.on_event = on_event
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.max_backoff = max_backoff
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._ws: WebSocketClientProtocol | None = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name=f"recall-ws-{self.bot_id}")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                await self._connect_and_consume()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except (WebSocketException, ConnectionClosed, OSError) as exc:
                if self._stop_event.is_set():
                    return
                logger.warning(
                    "recall realtime ws disconnected bot_id=%s err=%s; reconnecting in %.1fs",
                    self.bot_id,
                    exc,
                    backoff,
                )
                await self._sleep_or_stop(backoff)
                backoff = min(self.max_backoff, backoff * 2)
            except Exception:
                logger.exception(
                    "recall realtime ws crashed bot_id=%s; reconnecting in %.1fs", self.bot_id, backoff
                )
                await self._sleep_or_stop(backoff)
                backoff = min(self.max_backoff, backoff * 2)

    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return
        raise asyncio.CancelledError

    async def _connect_and_consume(self) -> None:
        url = f"{self.ws_url}?bot_id={self.bot_id}"
        headers = {"Authorization": self.api_key}
        logger.info("recall realtime ws connecting bot_id=%s url=%s", self.bot_id, url)
        async with websockets.connect(
            url,
            additional_headers=headers,
            ping_interval=self.ping_interval,
            ping_timeout=self.ping_timeout,
            max_size=32 * 1024 * 1024,
        ) as ws:
            self._ws = ws
            logger.info("recall realtime ws connected bot_id=%s", self.bot_id)
            async for raw in ws:
                if self._stop_event.is_set():
                    return
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    logger.warning("recall realtime ws non-json message bot_id=%s", self.bot_id)
                    continue
                event = str(payload.get("event") or "")
                if not event or event not in self.events:
                    continue
                data = payload.get("data") or {}
                try:
                    result = self.on_event(event, data)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    logger.exception(
                        "recall realtime ws on_event failed bot_id=%s event=%s", self.bot_id, event
                    )

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()
