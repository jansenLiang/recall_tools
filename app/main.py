from __future__ import annotations

import asyncio
import contextlib

from fastapi import FastAPI

from app.api.routes import router
from app.core.config import settings
from app.core.logging import configure_logging
from app.services.session_service import session_service


async def _bot_only_cleanup_loop() -> None:
    interval = max(1, settings.bot_only_cleanup_interval_seconds)
    while True:
        await asyncio.sleep(interval)
        await session_service.cleanup_bot_only_sessions()


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="Mirako Recall Tools API",
        description="Start and close Recall.ai meeting bridge sessions.",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )
    app.include_router(router)

    @app.on_event("startup")
    async def start_background_tasks() -> None:
        if settings.bot_only_cleanup_enabled:
            app.state.bot_only_cleanup_task = asyncio.create_task(
                _bot_only_cleanup_loop()
            )

    @app.on_event("shutdown")
    async def stop_background_tasks() -> None:
        task = getattr(app.state, "bot_only_cleanup_task", None)
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.app_host, port=settings.app_port, reload=True)
