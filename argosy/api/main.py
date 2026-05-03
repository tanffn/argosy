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

Phase 6 additions:
  - GET  /api/branding                — per-tenant theme tokens.
  - GET  /internal/health/full        — full watchdog signals (admin).
  - POST /internal/telemetry          — receiver stub (admin).
  - CORS now reads `ARGOSY_CORS_ORIGINS` env (comma-separated) so the
    same image can serve Vercel-hosted UIs in addition to localhost.
"""

from __future__ import annotations

import asyncio
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from argosy import __version__
from argosy.api.events import subscribe
from argosy.api.routes.agent_activity import router as agent_activity_router
from argosy.api.routes.argonaut import router as argonaut_router
from argosy.api.routes.branding import router as branding_router
from argosy.api.routes.daily_brief import router as daily_brief_router
from argosy.api.routes.decisions import router as decisions_router
from argosy.api.routes.domain_kb import router as domain_kb_router
from argosy.api.routes.execution import router as execution_router
from argosy.api.routes.health import router as health_router
from argosy.api.routes.intake import router as intake_router
from argosy.api.routes.internal import router as internal_router
from argosy.api.routes.onboarding import router as onboarding_router
from argosy.api.routes.plan import router as plan_router
from argosy.api.routes.portfolio import router as portfolio_router
from argosy.api.routes.proposals import router as proposals_router
from argosy.api.routes.security import router as security_router
from argosy.api.routes.settings import (
    cost_guard_router,
    router as settings_router,
)
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

    cors_env = os.environ.get("ARGOSY_CORS_ORIGINS", "")
    extra_origins = [o.strip() for o in cors_env.split(",") if o.strip()]
    cors_origins = [
        f"http://localhost:{settings.server.ui_port}",
        f"http://127.0.0.1:{settings.server.ui_port}",
        *extra_origins,
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
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

    # Phase 3 — proposals + decisions
    app.include_router(proposals_router, prefix=api_prefix)
    app.include_router(decisions_router, prefix=api_prefix)

    # Phase 4 — execution router + lots/fills/audit + email-link approval
    app.include_router(execution_router, prefix=api_prefix)

    # Phase 5 — Argonaut limited account + TOTP second-factor
    app.include_router(argonaut_router, prefix=api_prefix)
    app.include_router(security_router, prefix=api_prefix)

    # Phase 6 — branding + onboarding + admin/internal
    app.include_router(branding_router, prefix=api_prefix)
    app.include_router(onboarding_router, prefix=api_prefix)
    app.include_router(internal_router)

    # Phase 7 — domain KB browser, intake API, settings, cost-guard override
    app.include_router(domain_kb_router, prefix=api_prefix)
    app.include_router(intake_router, prefix=api_prefix)
    app.include_router(settings_router, prefix=api_prefix)
    app.include_router(cost_guard_router)

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
