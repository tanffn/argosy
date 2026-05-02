"""FastAPI application factory.

Phase 0 endpoints:
  - GET /health
  - WebSocket /ws — accepts, sends "connected", then idles.

CORS allows the Next.js dev server at http://localhost:1337.
"""

from __future__ import annotations

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from argosy import __version__
from argosy.api.routes.health import router as health_router
from argosy.config import get_settings
from argosy.logging import configure_logging, get_logger


def create_app() -> FastAPI:
    configure_logging()
    log = get_logger(__name__)
    settings = get_settings()

    app = FastAPI(
        title="Argosy API",
        version=__version__,
        description="Argosy: multi-agent financial advisor (Phase 0 scaffold)",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[f"http://localhost:{settings.server.ui_port}"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router)

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("connected")
        log.info("ws.client_connected")
        try:
            while True:
                # Phase 0: idle. Real events arrive in later phases.
                msg = await websocket.receive_text()
                # Echo so a developer can ping the socket if they want.
                await websocket.send_text(f"echo:{msg}")
        except WebSocketDisconnect:
            log.info("ws.client_disconnected")

    log.info("argosy.api.started", version=__version__, home=str(settings.home))
    return app


app = create_app()
