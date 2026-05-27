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
from starlette.websockets import WebSocketState
from fastapi.middleware.cors import CORSMiddleware

from argosy import __version__
from argosy.api.events import subscribe
from argosy.api.routes.advisor import router as advisor_router
from argosy.api.routes.agent_activity import router as agent_activity_router
from argosy.api.routes.argonaut import router as argonaut_router
from argosy.api.routes.branding import router as branding_router
from argosy.api.routes.daily_brief import router as daily_brief_router
from argosy.api.routes.decisions import router as decisions_router
from argosy.api.routes.decisions_tree import router as decisions_tree_router
from argosy.api.routes.domain_kb import router as domain_kb_router
from argosy.api.routes.execution import router as execution_router
from argosy.api.routes.files import router as files_router
from argosy.api.routes.fleet_self_review import router as fleet_self_review_router
from argosy.api.routes.health import router as health_router
from argosy.api.routes.intake import router as intake_router
from argosy.api.routes.internal import router as internal_router
from argosy.api.routes.onboarding import router as onboarding_router
from argosy.api.routes.plan import router as plan_router
from argosy.api.routes.plan_objection_state import (
    router as plan_objection_state_router,
)
from argosy.api.routes.fm_objection_dialogue import (
    router as fm_objection_dialogue_router,
)
from argosy.api.routes.portfolio import router as portfolio_router
from argosy.api.routes.positions import router as positions_router
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

    # Phase 0 — /health is exposed at both root and /api/health so it works
    # whether the caller goes through the Next.js /api/* proxy or hits the
    # FastAPI process directly (e.g., the watchdog liveness check).
    app.include_router(health_router)
    app.include_router(health_router, prefix="/api")

    # Phase 2 (multi-tenant `user_id` query param on each)
    api_prefix = "/api"
    app.include_router(portfolio_router, prefix=api_prefix)
    app.include_router(plan_router, prefix=api_prefix)
    # Per-FM-objection agree/disagree + start-new-round endpoints. Sibling
    # router so the agree/disagree work doesn't have to share plan.py with
    # concurrent edits (translation cache, NVDA PACE). Same /plan/draft/
    # objections/* URL prefix so the UI doesn't need to distinguish.
    app.include_router(plan_objection_state_router, prefix=api_prefix)
    # FM-objection ZigZag — slim FM↔analyst dialogue per objection. Sibling
    # router so the dialogue feature can ship without rebasing on
    # concurrent plan.py edits. Same /plan/draft/objections/* URL prefix.
    app.include_router(fm_objection_dialogue_router, prefix=api_prefix)
    # T4.1 — per-position thesis cards. Sibling router so it doesn't have
    # to share the plan router's get_db dependency wiring.
    app.include_router(positions_router, prefix=api_prefix)
    app.include_router(daily_brief_router, prefix=api_prefix)
    app.include_router(agent_activity_router, prefix=api_prefix)

    # Phase 3 — proposals + decisions
    app.include_router(proposals_router, prefix=api_prefix)
    app.include_router(decisions_router, prefix=api_prefix)
    # T0.5 — FM-rooted agent-tree view, mounted as a sibling router so it
    # doesn't have to share a get_db pattern with the async decisions
    # router. Same /decisions prefix, distinct path => no conflict.
    app.include_router(decisions_tree_router, prefix=api_prefix)

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
    # Phase 1 reframe — advisor panel (gap-tracker + persistent Q&A).
    # Mounted alongside /api/intake/* so the legacy intake page keeps
    # working until the redirect lands.
    app.include_router(advisor_router, prefix=api_prefix)
    app.include_router(settings_router, prefix=api_prefix)
    app.include_router(cost_guard_router)

    # Provenance Wave A — user-files catalog list/stream surface.
    app.include_router(files_router, prefix=api_prefix)

    # Fleet self-review (migration 0037) — surfaces anomalies the user
    # shouldn't have to find by hand.  Auto-fires from the plan_synthesis
    # orchestrator on every completion + from the daily-brief loop on
    # the daily sweep.  See argosy/services/fleet_self_review.py for
    # detector implementations.
    app.include_router(fleet_self_review_router, prefix=api_prefix)

    # T2.2 — startup orphan sweep. Mark any decision_runs that are still
    # status='running' from a prior process that was killed mid-flight as
    # 'failed' with a structured note so the audit trail is honest and
    # the home page doesn't render forever-running rows. Runs synchronously
    # at create_app() time so the first /api/decisions/recent request after
    # a restart already sees the cleaned state.
    @app.on_event("startup")
    async def _orphan_sweep_at_startup() -> None:
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import update

        from argosy.state import db as db_mod
        from argosy.state.models import DecisionRun

        try:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=4)
            # Compare on naive UTC (SQLite stores naive; new rows persist
            # tz-aware UTC but DateTime column strips on read).
            cutoff_naive = cutoff.replace(tzinfo=None)
            async with db_mod.get_session() as session:
                result = await session.execute(
                    update(DecisionRun)
                    .where(DecisionRun.status == "running")
                    .where(DecisionRun.started_at < cutoff_naive)
                    .values(
                        status="failed",
                        finished_at=datetime.now(timezone.utc),
                        notes_json='{"orphaned_by": "uvicorn_restart", "cutoff_hours": 4}',
                    )
                )
                await session.commit()
                if result.rowcount:
                    log.info(
                        "orphan_sweep.swept",
                        count=result.rowcount,
                        cutoff_iso=cutoff.isoformat(),
                    )
        except Exception as exc:  # noqa: BLE001 — must NEVER block startup
            log.warning("orphan_sweep.failed", error=str(exc))

    # Expenses Wave EX1 — household-expenses ingest surface.
    from argosy.api.routes import expenses as expenses_routes
    app.include_router(expenses_routes.router)

    # T4.5 — daily-brief production loop. Off by default; opt-in via
    # ARGOSY_DAILY_BRIEF_ENABLED=1. Always off under pytest so tests
    # never spawn a real background fire. The loop sleeps until 07:00
    # in Asia/Jerusalem, then fires generate_daily_brief() once and
    # sleeps again. See argosy/services/daily_brief_runner.py.
    @app.on_event("startup")
    async def _start_daily_brief_loop() -> None:
        from argosy.services.daily_brief_runner import (
            background_loop,
            is_enabled_for_runtime,
        )

        if not is_enabled_for_runtime():
            log.info("daily_brief_runner.disabled")
            return
        log.info("daily_brief_runner.scheduled", tz="Asia/Jerusalem", at="07:00")
        asyncio.create_task(background_loop(), name="daily_brief_runner")

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
                    # T2.5 — check the client_state before sending. The
                    # ASGI spec rejects sends after a 'websocket.close'
                    # has been sent, which manifests as the
                    # `Unexpected ASGI message 'websocket.send' after
                    # sending 'websocket.close'` storm. The recv_task
                    # detects disconnects and propagates here via the
                    # state flip, so we can quietly break out instead
                    # of raising N times for N pending queue items.
                    if websocket.client_state != WebSocketState.CONNECTED:
                        log.info(
                            "ws.client_disconnected_during_send",
                            client_state=str(websocket.client_state),
                        )
                        break
                    try:
                        await websocket.send_text(msg)
                    except (WebSocketDisconnect, RuntimeError) as exc:
                        # WebSocketDisconnect: explicit close from client.
                        # RuntimeError: the ASGI "send after close"
                        # message — same root cause, race between the
                        # disconnect detection and the next queue item.
                        # Treat both as a clean close and bail.
                        log.info(
                            "ws.send_after_close",
                            error_type=type(exc).__name__,
                        )
                        break
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
