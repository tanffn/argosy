"""FastAPI application factory.

Phase 0 endpoints:
  - GET /health
  - WebSocket /ws — accepts, sends "connected", then idles.

Phase 2 endpoints:
  - GET  /api/portfolio/snapshot
  - GET  /api/plan/current
  - POST /api/plan/critique
  - GET  /api/daily-brief/latest
  - GET  /api/agent-activity
  - WS   /ws — pushes `daily_brief.ready` and `agent.run.finished` events.

CORS allows the Next.js dev server at http://localhost:1337.
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from argosy import __version__
from argosy.api.events import subscribe
from argosy.api.routes.agent_activity import router as agent_activity_router
from argosy.api.routes.daily_brief import router as daily_brief_router
from argosy.api.routes.health import router as health_router
from argosy.api.routes.plan import router as plan_router
from argosy.api.routes.portfolio import router as portfolio_router
from argosy.config import get_settings
from argosy.logging import configure_logging, get_logger


def create_app() -> FastAPI:
    configure_logging()
    log = get_logger(__name__)
    settings = get_settings()

    app = FastAPI(
        title="Argosy API",
        version=__version__,
        description="Argosy: multi-agent financial advisor (Phase 2)",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[f"http://localhost:{settings.server.ui_port}"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Phase 0
    app.include_router(health_router)

    # Phase 2 (multi-tenant `user_id` query param on each)
    api_prefix = "/api"
    app.include_router(portfolio_router, prefix=api_prefix)
    app.include_router(plan_router, prefix=api_prefix)
    app.include_router(daily_brief_router, prefix=api_prefix)
    app.include_router(agent_activity_router, prefix=api_prefix)

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("connected")
        log.info("ws.client_connected")

        # Subscribe to the in-process event bus and forward each event to
        # the client. We also keep a recv pump alive so client disconnects
        # are detected promptly.
        async with subscribe() as q:
            recv_task = asyncio.create_task(_recv_pump(websocket))
            try:
                while True:
                    msg = await q.get()
                    await websocket.send_text(msg)
            except WebSocketDisconnect:
                log.info("ws.client_disconnected")
            except Exception:  # pragma: no cover - defensive
                log.exception("ws.send_failed")
            finally:
                recv_task.cancel()
                try:
                    await recv_task
                except (asyncio.CancelledError, Exception):
                    pass

    log.info("argosy.api.started", version=__version__, home=str(settings.home))
    return app


async def _recv_pump(websocket: WebSocket) -> None:
    """Drain inbound messages so disconnects surface promptly. Echoes pings."""
    try:
        while True:
            msg = await websocket.receive_text()
            # Light echo for dev convenience.
            await websocket.send_text(f"echo:{msg}")
    except WebSocketDisconnect:
        return


app = create_app()
