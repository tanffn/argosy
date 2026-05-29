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
from argosy.api.routes.wealth_dashboard import router as wealth_dashboard_router
from argosy.api.routes.positions import router as positions_router
from argosy.api.routes.allocation import router as allocation_router
from argosy.api.routes.life_events import router as life_events_router
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
    # Wealth Dashboard — top-of-/portfolio retirement projection + 6
    # visual stat cards (cash runway, concentration, savings rate, FX
    # exposure, RSU income, estate exposure). Sibling router so it
    # doesn't have to share portfolio.py's filesystem-walk logic.
    app.include_router(wealth_dashboard_router, prefix=api_prefix)
    app.include_router(plan_router, prefix=api_prefix)
    # Retirement-companion engine — umbrella router for the 30-gap overhaul.
    # Plan: docs/superpowers/plans/2026-05-28-retirement-companion-overhaul.md
    from argosy.api.routes.retirement import router as retirement_router
    app.include_router(retirement_router, prefix=api_prefix)
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
    # Generic Accept/Defer for allocation-action proposals (sprint commit
    # #6b). Mounts at /api/proposals/allocation/* — sibling to the
    # /api/proposals/* trade-order routes. Generalizes the windfall
    # accept/defer pattern over the action_source discriminator added
    # in migration 0041.
    app.include_router(allocation_router, prefix=api_prefix)
    # /life-events page (sprint commit #8) — structured intake for the
    # life-stage data that feeds effective_retire_ready_age() clamps +
    # the Holistic Timeline. Pydantic enum validation + loud-error 422
    # contract per codex BLOCKER on spec #1 §4.1.
    app.include_router(life_events_router, prefix=api_prefix)
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

    # EX2 anomaly-detection report viewer (migration 0038). Auto-fires
    # from the expense ingest path on every Discount Bank statement
    # AND from the daily-brief loop as a backstop. See
    # argosy/services/anomaly_runner.py and
    # argosy/agents/anomaly_detection.py for the agent and runner.
    from argosy.api.routes.anomalies import router as anomalies_router
    app.include_router(anomalies_router, prefix=api_prefix)

    # Sprint #2 commits #10-#11 — anomaly highlights + inline badges.
    # Distinct from the /anomalies (plural) router above: that one
    # serves the full anomaly_reports document; this one consumes the
    # per-row expense_review_queue rows written by the sprint #2
    # detectors in argosy/services/anomaly/ and formats them as
    # AnomalyCards for the Home tile + inline transaction badges.
    from argosy.api.routes.anomaly import router as anomaly_router
    app.include_router(anomaly_router, prefix=api_prefix)

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

    # Sprint A commit #4 — /api/jobs routes + admin auth gate.
    # ``register_routers`` mounts the open GET routes unconditionally
    # and ONLY mounts the mutating POST routes when ARGOSY_ADMIN_TOKEN
    # is set in the environment (BLOCKER #1 — mutating routes refuse to
    # mount when the env var is absent). The refusal happens HERE, at
    # app construction time, not on each request — so a misconfigured
    # token can't be brute-forced.
    from argosy.api.routes.jobs import register_routers as _register_jobs_routers
    _register_jobs_routers(app)

    # Sprint A commit #3b — JobRegistry + RegisteredScheduler lifecycle.
    # Spec: docs/superpowers/specs/2026-05-29-jobs-registry-design.md
    # §1.2 (lifecycle) + commit #3b. The registry is ALWAYS constructed
    # (so /api/jobs in commit #4 can serve a stale-but-readable view from
    # cadence_state even when ARGOSY_RUN_SCHEDULER=0). The scheduler +
    # run_forever task are only booted when the env gate permits.
    #
    # Env precedence (codex NICE #6, spec §Commit #3b):
    #   ARGOSY_RUN_SCHEDULER unset → boot (default "1")
    #   ARGOSY_RUN_SCHEDULER=1 → boot
    #   ARGOSY_RUN_SCHEDULER=0 → skip boot, log WARNING
    @app.on_event("startup")
    async def _start_jobs_scheduler() -> None:
        from argosy.services.jobs import JobRegistry
        from argosy.services.jobs.registered_scheduler import (
            RegisteredScheduler,
        )

        # Registry is constructed unconditionally so /api/jobs (commit
        # #4) has SOMETHING to read even when the scheduler is off.
        registry = JobRegistry()
        app.state.job_registry = registry

        run_flag = os.environ.get("ARGOSY_RUN_SCHEDULER", "1").strip()
        if run_flag == "0":
            log.warning(
                "scheduler.disabled",
                reason="ARGOSY_RUN_SCHEDULER=0",
                note="scheduler disabled; jobs will not run automatically",
            )
            app.state.scheduler = None
            app.state.scheduler_task = None
            return

        # Build the scheduler with the registry bound so every
        # _fire_once flows through the JobRegistry's audit recorder.
        # Ordering note (codex review focus): we MUST finish all
        # registration (register_default_loops + optional long-running
        # jobs) BEFORE handing the scheduler to run_forever — otherwise
        # the first tick could race a half-built registry.
        # Scheduler defaults user_id="ariel" — the single-user
        # convention. When Argosy goes multi-tenant the scheduler will
        # be instantiated per-user, not per-process.
        scheduler = RegisteredScheduler(registry=registry)
        registry.bind_scheduler(scheduler)

        # Register the cadence loops set (weekly_review, reconcile,
        # minute/hour/monthly/quarterly/annual/backup/audit/watchlist/
        # plan_watcher — gated on agent_settings.yaml cadence flags).
        scheduler.register_default_loops()

        # Sprint A commit #9 — JobRunsRetentionLoop. Gated on
        # ``cadences.job_runs_retention.enabled`` (default True).
        # Registered alongside the default cadence loops so it appears
        # in /api/jobs as a regular maintenance-kind job.
        #
        # Codex review IMPORTANT #3 — narrow exception handling.
        # Imports + agent_settings load can raise ImportError /
        # ValidationError; loop construction can raise ValueError on
        # negative windows. We catch those explicitly and log; any
        # OTHER exception (e.g. a programmer error introduced by a
        # later refactor) propagates so it fails loudly at startup.
        try:
            from argosy.agent_settings import (  # noqa: PLC0415
                load_agent_settings,
            )
            from argosy.orchestrator.loops.base import (  # noqa: PLC0415
                LoopSchedule,
            )
            from argosy.orchestrator.loops.job_runs_retention import (  # noqa: PLC0415
                JobRunsRetentionLoop,
                job_runs_retention_metadata,
            )

            agent_cfg = load_agent_settings("ariel")
            ret_cad = agent_cfg.cadences.job_runs_retention
            if ret_cad.enabled:
                # Codex BLOCKER #1 — thread the current set of
                # LongRunningJob names so the reap pass can exclude
                # them. Reads ``registry._jobs`` at TICK TIME (not at
                # construction) so a LongRunningJob registered after
                # the retention loop (commits #6 + #7 import-guarded
                # blocks below) is still seen.
                def _long_running_names() -> list[str]:
                    return [
                        name
                        for name, rec in registry._jobs.items()
                        if rec.metadata.long_running
                    ]

                retention_job = JobRunsRetentionLoop(
                    schedule=LoopSchedule.from_config(ret_cad),
                    enabled=True,
                    retention_days_ok=(
                        agent_cfg.job_runs_retention.retention_days_ok
                    ),
                    stale_running_hours=(
                        agent_cfg.job_runs_retention.stale_running_hours
                    ),
                    long_running_names_fn=_long_running_names,
                )
                scheduler.register_loop(retention_job)
                registry.register(
                    job=retention_job,
                    metadata=job_runs_retention_metadata(),
                )
                log.info("scheduler.job_runs_retention_registered")
        except (ImportError, ValueError) as exc:
            log.exception(
                "scheduler.job_runs_retention_register_failed",
                error_type=type(exc).__name__,
            )

        # Commit #6 (DiscordListenerJob) + commit #7 (NewsDailyJob)
        # land later in the sprint. Gate their imports so #3b can land
        # clean before them. When they're available, register them
        # with the registry so the supervisor (commit #5) picks them
        # up on start_supervisors().
        try:  # pragma: no cover - exercised via FastAPI startup, not unit-tested directly
            from argosy.services.jobs.discord_listener_job import (
                DiscordListenerJob,
                discord_listener_metadata,
            )
            from argosy.services.discord_listener import load_creds

            # Try-load creds — None when ~/.argosy/discord_creds.json
            # is missing; raises ValueError when malformed. We log the
            # malformed case but still register the job with creds=None
            # so the admin UI shows "creds missing" rather than the row
            # vanishing entirely.
            try:
                _creds = load_creds()
            except ValueError as exc:
                log.warning("discord_listener.creds.malformed", error=str(exc))
                _creds = None

            # Build a sync session factory shared with the listener
            # body (same shape ``argosy discord-ingest`` uses).
            from argosy.cli.discord_ingest import _build_session_factory
            _discord_session_factory = _build_session_factory()

            discord_job = DiscordListenerJob(
                _creds, _discord_session_factory,
            )
            registry.register(
                job=discord_job, metadata=discord_listener_metadata()
            )
        except ImportError:
            pass

        try:  # pragma: no cover - lands in commit #7
            from argosy.services.jobs.news_daily import (  # type: ignore[import-not-found]
                NewsDailyJob,
                news_daily_metadata,
            )

            news_job = NewsDailyJob()
            scheduler.register_loop(news_job)
            registry.register(job=news_job, metadata=news_daily_metadata())
        except ImportError:
            pass

        # Sprint B commit #7 — StateObserverLoop. Gated on
        # ``cadences.state_observer.enabled`` (default True). 17:00 IDT
        # daily alongside news_daily, source_kind='monitor'.
        try:
            from argosy.agent_settings import (  # noqa: PLC0415
                load_agent_settings,
            )
            from argosy.orchestrator.loops.base import (  # noqa: PLC0415
                LoopSchedule,
            )
            from argosy.orchestrator.loops.state_observer import (  # noqa: PLC0415
                StateObserverLoop,
                state_observer_metadata,
            )

            obs_cfg = load_agent_settings("ariel").cadences.state_observer
            if obs_cfg.enabled:
                observer_loop = StateObserverLoop(
                    schedule=LoopSchedule.from_config(obs_cfg),
                    enabled=True,
                    user_id="ariel",
                )
                scheduler.register_loop(observer_loop)
                registry.register(
                    job=observer_loop,
                    metadata=state_observer_metadata(),
                )
                log.info("scheduler.state_observer_registered")
        except (ImportError, ValueError) as exc:
            log.exception(
                "scheduler.state_observer_register_failed",
                error_type=type(exc).__name__,
            )

        # Sprint C commit #4 — PredictionsEvaluatorLoop. Gated on
        # ``cadences.predictions_evaluator.enabled`` (default True).
        # 03:30 IDT daily alongside job_runs_retention — both run
        # against disjoint rows so the schedule overlap is intentional
        # (one less cron entry for the operator to reason about).
        # source_kind='maintenance' so it lives in the same admin-UI
        # family as the retention / backup loops.
        try:
            from argosy.agent_settings import (  # noqa: PLC0415
                load_agent_settings,
            )
            from argosy.orchestrator.loops.base import (  # noqa: PLC0415
                LoopSchedule,
            )
            from argosy.orchestrator.loops.predictions_evaluator import (  # noqa: PLC0415
                PredictionsEvaluatorLoop,
                predictions_evaluator_metadata,
            )

            pe_cfg = load_agent_settings(
                "ariel"
            ).cadences.predictions_evaluator
            if pe_cfg.enabled:
                evaluator_loop = PredictionsEvaluatorLoop(
                    schedule=LoopSchedule.from_config(pe_cfg),
                    enabled=True,
                )
                scheduler.register_loop(evaluator_loop)
                registry.register(
                    job=evaluator_loop,
                    metadata=predictions_evaluator_metadata(),
                )
                log.info("scheduler.predictions_evaluator_registered")
        except (ImportError, ValueError) as exc:
            log.exception(
                "scheduler.predictions_evaluator_register_failed",
                error_type=type(exc).__name__,
            )

        # Step 1: start_supervisors BEFORE scheduling so any
        # LongRunningJob supervisor is alive when its first connect
        # cycle opens. (No-op until commit #5 fills it in.)
        await registry.start_supervisors()

        # Step 2: spawn the scheduler's run_forever task. Stashed on
        # app.state so shutdown can join it.
        task = asyncio.create_task(
            scheduler.run_forever(), name="argosy_scheduler"
        )
        app.state.scheduler = scheduler
        app.state.scheduler_task = task
        log.info(
            "scheduler.started",
            registered_loops=list(scheduler._loops.keys()),
            registered_jobs=registry.names(),
        )

    @app.on_event("shutdown")
    async def _stop_jobs_scheduler() -> None:
        """Drain the scheduler within 5s; suppress TimeoutError with a
        warning so a stuck Discord listener (commit #6) can't hold up
        the FastAPI shutdown.
        """
        scheduler = getattr(app.state, "scheduler", None)
        task = getattr(app.state, "scheduler_task", None)
        registry = getattr(app.state, "job_registry", None)

        if scheduler is not None:
            # stop() sets the _stop event; run_forever cancels per-loop
            # tasks and gathers them. The 5s join below bounds how long
            # we'll wait for that gather to finish.
            scheduler.stop()

        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except asyncio.TimeoutError:
                log.warning(
                    "scheduler.shutdown_join_timeout",
                    timeout_s=5.0,
                    note="run_forever did not drain within 5s; abandoning",
                )
                task.cancel()
            except asyncio.CancelledError:  # pragma: no cover - defensive
                pass
            except Exception:  # pragma: no cover - defensive
                log.exception("scheduler.shutdown_join_failed")

        if registry is not None:
            try:
                await registry.stop_supervisors()
            except Exception:  # pragma: no cover - defensive
                log.exception("scheduler.stop_supervisors_failed")

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
