from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from argosy.orchestrator.loops.base import CadenceLoop, LoopSchedule
from argosy.services.decision_readiness import collect_decision_readiness, RECOVERABLE_JOBS
from argosy.services.jobs import JobRegistry, JobMetadata, RegisteredScheduler
from argosy.state import db as db_mod
from argosy.state.models import JobRun, NewsSignal


def test_recovery_names_match_actual_monitor_registrations():
    from argosy.orchestrator.loops.state_observer import StateObserverLoop
    from argosy.orchestrator.loops.thesis_monitor import ThesisMonitorLoop
    from argosy.services.jobs.earnings_calendar_daily import EarningsCalendarDailyJob
    from argosy.services.jobs.sec_earnings_daily import SecEarningsDailyJob
    from argosy.orchestrator.loops.signal_streams_daily import SignalStreamsDailyLoop

    assert StateObserverLoop.name in RECOVERABLE_JOBS
    assert ThesisMonitorLoop.name in RECOVERABLE_JOBS
    assert {EarningsCalendarDailyJob.name, SecEarningsDailyJob.name,
            SignalStreamsDailyLoop.name} <= set(RECOVERABLE_JOBS)
    assert not {"annual", "process_cooling", "weekly_email_digest", "discord_listener"} & set(RECOVERABLE_JOBS)


async def receipt(name, *, now, status="error", error="model unavailable", summary=None):
    async with db_mod.get_session() as session:
        session.add(JobRun(
            job_name=name, started_at=now, finished_at=now, status=status,
            error_message=error, output_summary=summary, manual_trigger=0,
            triggered_by="scheduler", idempotency_key=uuid4().hex,
        ))
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("urgency,matches,expected", [
    ("routine", True, "ready"), ("urgent", True, "blocked"), ("urgent", False, "ready"),
])
async def test_knowledge_completeness_is_separate_from_urgency(engine, monkeypatch, urgency, matches, expected):
    from argosy.services import knowledge_status
    from argosy.state.models import User, UserContext
    async def incomplete(*args, **kwargs):
        return {"status": "incomplete", "documents": [{"path": "rule.md", "input_matches": matches,
                "findings": [{"urgency": urgency, "claim": "Specific risk"}]}]}
    monkeypatch.setattr(knowledge_status, "collect_knowledge_status", incomplete)
    now = datetime.now(UTC)
    for name in (*RECOVERABLE_JOBS, "annual"):
        await receipt(name, now=now, status="ok", error=None, summary="{}")
    async with db_mod.get_session() as session:
        await session.merge(User(id="ariel"))
        await session.merge(UserContext(user_id="ariel", constraints_yaml=
            'knowledge_followups:\n  paperwork_due_date: "2026-12-31"\n'))
        await session.commit()
        state = await collect_decision_readiness(session, user_id="ariel", now=now)
        assert state["status"] == expected
        assert bool(state["urgent_findings"]) == (expected == "blocked")
        assert state["knowledge"]["paperwork_due_date"] == "2026-12-31"
        assert not next(j for j in state["jobs"] if j["name"] == "annual")["attention_required"]
        other = await collect_decision_readiness(session, user_id="someone_else", now=now)
        assert other["knowledge"]["paperwork_due_date"] is None
        context = await session.get(UserContext, "ariel")
        context.constraints_yaml = 'knowledge_followups: [malformed]'
        await session.flush()
        malformed = await collect_decision_readiness(session, user_id="ariel", now=now)
        assert malformed["knowledge"]["paperwork_due_date"] is None


@pytest.mark.asyncio
async def test_readiness_reports_provider_outage_and_backlog(engine):
    now = datetime.now(UTC)
    await receipt("news_daily", now=now, error="Your organization requires remote managed settings to load")
    async with db_mod.get_session() as session:
        session.add(NewsSignal(
            source="rss", source_ref="outage-pending", received_at=now,
            evidence_excerpt="pending", raw_text="pending", source_trust="medium", sentiment="neutral",
        ))
        await session.commit()
        state = await collect_decision_readiness(session, user_id="ariel", now=now)
    assert state["status"] == "blocked"
    assert state["pending_news"] == 1
    assert "not verified" in state["message"]
    job = next(j for j in state["jobs"] if j["name"] == "news_daily")
    assert job["status"] == "error"
    assert "Sign in to Claude again" in job["failure"]["action"]


@pytest.mark.asyncio
async def test_readiness_requires_current_success_not_merely_an_attempt(engine):
    now = datetime.now(UTC)
    for name in RECOVERABLE_JOBS:
        await receipt(name, now=now-timedelta(days=3), status="ok", error=None)
    async with db_mod.get_session() as session:
        state = await collect_decision_readiness(session, user_id="ariel", now=now)
    assert state["status"] == "degraded"
    assert all(j["status"] == "stale" for j in state["jobs"])
    for name in RECOVERABLE_JOBS:
        await receipt(name, now=now, status="ok", error=None)
    async with db_mod.get_session() as session:
        state = await collect_decision_readiness(session, user_id="ariel", now=now)
    assert state["status"] == "ready"


@pytest.mark.asyncio
async def test_legacy_funnel_failure_is_not_reported_as_last_success(engine):
    import json
    from argosy.state.models import FunnelRun, FunnelStageRow, User

    now = datetime.now(UTC)
    async with db_mod.get_session() as session:
        if await session.get(User, "ariel") is None:
            session.add(User(id="ariel", plan="free"))
            await session.flush()
        run = FunnelRun(user_id="ariel", started_at=now, finished_at=now,
                        status="ok", shadow=0, trigger="scheduler", idempotency_key=uuid4().hex)
        session.add(run)
        await session.flush()
        run_id = run.id
        session.add(FunnelStageRow(run_id=run_id, stage="stage2", subject="ABCL",
                                  subject_type="discovery", decision="triage_error", reason="runtime unavailable"))
        await session.commit()
    await receipt("decision_funnel", now=now, status="ok", error=None, summary=json.dumps({"run_id": run_id}))
    async with db_mod.get_session() as session:
        report = await collect_decision_readiness(session, user_id="ariel", now=now)
    job = next(j for j in report["jobs"] if j["name"] == "decision_funnel")
    assert job["status"] == "error"
    assert job["last_success_at"] is None
    # A later fixed-code successful retry of the same daily funnel may retain
    # old append-only stage rows, but current aggregate totals are authoritative.
    async with db_mod.get_session() as session:
        run = await session.get(FunnelRun, run_id)
        run.totals_json = '{"status": "ok", "error_count": 0}'
        await session.commit()
        report = await collect_decision_readiness(session, user_id="ariel", now=now)
    assert next(j for j in report["jobs"] if j["name"] == "decision_funnel")["status"] == "ok"


class AnalysisLoop(CadenceLoop):
    name = "news_daily"

    def __init__(self, now):
        due = now - timedelta(hours=2)
        super().__init__(enabled=True, schedule=LoopSchedule(cron=f"{due.minute} {due.hour} * * *", timezone="UTC"))
        self.calls = 0

    async def tick(self, *, now=None):
        self.calls += 1
        return {"status": "ok", "analyzed": 1}


def wire(loop, now):
    registry = JobRegistry()
    scheduler = RegisteredScheduler(registry=registry, settings=SimpleNamespace(), clock=lambda: now)
    registry.bind_scheduler(scheduler)
    scheduler.register_loop(loop)
    registry.register(job=loop, metadata=JobMetadata(
        name=loop.name, schedule_cron=loop.schedule.cron, schedule_human="daily",
        source_kind="monitor", description="analysis recovery test", long_running=False,
    ))
    return scheduler, registry


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["news_daily", "earnings_calendar_daily", "sec_earnings_daily", "signal_streams_daily", "period_directive_daily", "verdict_trigger_daily"])
async def test_recovery_uses_real_tick_and_persistent_receipts_after_restart(engine, name):
    now = datetime.now(UTC)
    await receipt(name, now=now-timedelta(minutes=40))
    loop = AnalysisLoop(now)
    loop.name = name
    scheduler, _ = wire(loop, now)
    await scheduler._recover_failed_jobs()
    assert loop.calls == 1
    async with db_mod.get_session() as session:
        rows = list((await session.execute(select(JobRun).order_by(JobRun.id))).scalars())
    assert rows[-1].status == "ok"
    assert rows[-1].triggered_by == "recovery"
    restarted, _ = wire(loop, now)
    await restarted._recover_failed_jobs()
    assert loop.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["news_daily", "earnings_calendar_daily", "sec_earnings_daily", "signal_streams_daily", "period_directive_daily"])
async def test_recovery_limits_survive_restart_and_do_not_touch_execution(engine, name):
    now = datetime.now(UTC)
    for i in range(3):
        await receipt(name, now=now-timedelta(minutes=50-i))
    await receipt("process_cooling", now=now-timedelta(minutes=40))
    loop = AnalysisLoop(now)
    loop.name = name
    scheduler, _ = wire(loop, now)
    await scheduler._recover_failed_jobs()
    assert loop.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("cause,calls", [("reaped: prior backend process exited", 1), ("user cancelled", 0)])
async def test_directive_recovers_process_exit_not_user_cancellation(engine, cause, calls):
    now = datetime.now(UTC)
    await receipt("period_directive_daily", now=now-timedelta(minutes=40), status="cancelled", error=cause)
    loop = AnalysisLoop(now)
    loop.name = "period_directive_daily"
    scheduler, _ = wire(loop, now)
    await scheduler._recover_failed_jobs()
    assert loop.calls == calls


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["unfinished", "research timed out", "getaddrinfo failed", "rate limit reached", "database is locked"])
@pytest.mark.parametrize("automatic,age_minutes,expected", [(True, 0, "research_recovery_pending"), (False, 0, "error"), (True, 180, "error")])
async def test_research_partial_success_is_distinct_but_exhaustion_or_stall_is_error(engine, automatic, age_minutes, expected, error):
    import json
    now = datetime.now(UTC)
    summary = {"status": "degraded", "proposal_id": 229, "order_sheet_fingerprint": "receipt",
        "materialization_status": "awaiting_unified_approval", "research": {"failures": [error],
        "recovery": [{"automatic_retry": automatic, "next_retry_at": (now-timedelta(minutes=age_minutes)).isoformat()}]}}
    await receipt("period_directive_daily", now=now, summary=json.dumps(summary))
    async with db_mod.get_session() as session:
        state = await collect_decision_readiness(session, user_id="ariel", now=now)
    job = next(j for j in state["jobs"] if j["name"] == "period_directive_daily")
    assert job["status"] == expected and job["recorded_status"] == "error"
    assert job["failure"]
    if expected == "research_recovery_pending":
        assert "Trade plan generated" in job["failure"]["reason"]


@pytest.mark.asyncio
async def test_scheduler_budget_exhaustion_cannot_promise_research_recovery(engine):
    import json
    now = datetime.now(UTC)
    summary = {"status": "degraded", "proposal_id": 229, "order_sheet_fingerprint": "receipt",
        "materialization_status": "awaiting_unified_approval", "research": {"failures": ["unfinished"],
        "recovery": [{"automatic_retry": True, "attempts_today": 1, "next_retry_at": now.isoformat()}]}}
    for _ in range(3):
        await receipt("period_directive_daily", now=now, summary=json.dumps(summary))
    async with db_mod.get_session() as session:
        state = await collect_decision_readiness(session, user_id="ariel", now=now)
    job = next(j for j in state["jobs"] if j["name"] == "period_directive_daily")
    assert job["status"] == "error"
    assert job["failure"]["code"] == "allocation_research_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("blocking,code", [("authentication_error", "authentication"), ("monthly budget exceeded", "budget"), ("remote managed settings unavailable", "claude_sign_in")])
async def test_mixed_research_failures_preserve_blocking_provider_cause(engine, blocking, code):
    import json
    now = datetime.now(UTC)
    summary = {"status": "degraded", "proposal_id": 229, "order_sheet_fingerprint": "receipt",
        "materialization_status": "awaiting_unified_approval", "research": {"failures": ["getaddrinfo failed", blocking],
        "recovery": [{"automatic_retry": True, "next_retry_at": now.isoformat()}]}}
    await receipt("period_directive_daily", now=now, summary=json.dumps(summary))
    async with db_mod.get_session() as session:
        state = await collect_decision_readiness(session, user_id="ariel", now=now)
    job = next(j for j in state["jobs"] if j["name"] == "period_directive_daily")
    assert job["status"] == "error" and job["failure"]["code"] == code


@pytest.mark.asyncio
async def test_dns_summary_cooldown_then_success_clears_failure(engine):
    now = datetime.now(UTC)
    loop = AnalysisLoop(now)
    loop.name = "sec_earnings_daily"
    scheduler, _ = wire(loop, now)
    await receipt(loop.name, now=now-timedelta(minutes=40))
    # Scheduler error_message is generic; the real DNS error is nested.
    await receipt("earnings_calendar_daily", now=now,
                  error="tick returned without raising but reported failure: failures=15",
                  summary='{"errors":[{"error":"[Errno 11001] getaddrinfo failed: private-host"}]}')
    await scheduler._recover_failed_jobs()
    assert loop.calls == 0
    async with db_mod.get_session() as session:
        state = await collect_decision_readiness(session, user_id="ariel", now=now)
    failed = next(j for j in state["jobs"] if j["name"] == "earnings_calendar_daily")
    assert failed["failure"]["code"] == "network_dns"
    assert "private-host" not in str(state)
    # Readiness must not infer recovery just because time has elapsed.
    later = now + timedelta(minutes=31)
    scheduler.clock = lambda: later
    await scheduler._recover_failed_jobs()
    assert loop.calls == 1
    async with db_mod.get_session() as session:
        state = await collect_decision_readiness(session, user_id="ariel", now=later)
    assert next(j for j in state["jobs"] if j["name"] == loop.name)["status"] == "ok"
    assert next(j for j in state["jobs"] if j["name"] == "earnings_calendar_daily")["status"] == "error"


@pytest.mark.asyncio
async def test_recovery_respects_lock_and_shared_outage_cooldown(engine):
    now = datetime.now(UTC)
    await receipt("news_daily", now=now-timedelta(minutes=40))
    loop = AnalysisLoop(now)
    scheduler, registry = wire(loop, now)
    async with registry._lock_for("news_daily"):
        await scheduler._recover_failed_jobs()
    assert loop.calls == 0
    await receipt("holdings_review", now=now, error="remote managed settings unavailable")
    await scheduler._recover_failed_jobs()
    assert loop.calls == 0


@pytest.mark.asyncio
async def test_recovery_never_runs_without_a_durable_attempt(engine, monkeypatch):
    now = datetime.now(UTC)
    await receipt("news_daily", now=now-timedelta(minutes=40))
    loop = AnalysisLoop(now)
    scheduler, registry = wire(loop, now)

    async def cannot_audit(**kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(registry, "_open_job_run", cannot_audit)
    await scheduler._recover_failed_jobs()
    await scheduler._recover_failed_jobs()
    assert loop.calls == 0


@pytest.mark.asyncio
async def test_summary_failure_preserves_shared_dependency_diagnosis(engine):
    now = datetime.now(UTC)
    loop = AnalysisLoop(now)

    async def failed_tick(**kwargs):
        return {"status": "error", "errors": ["remote managed settings could not be loaded"]}

    loop.tick = failed_tick
    scheduler, registry = wire(loop, now)
    await registry.fire_now(loop.name, triggered_by="test")
    async with db_mod.get_session() as session:
        state = await collect_decision_readiness(session, user_id="ariel", now=now)
    assert state["status"] == "blocked"
    other = AnalysisLoop(now)
    other.name = "holdings_review"
    scheduler.register_loop(other)
    registry.register(job=other, metadata=JobMetadata(
        name=other.name, schedule_cron=other.schedule.cron, schedule_human="daily",
        source_kind="monitor", description="shared cooldown test", long_running=False,
    ))
    await receipt("holdings_review", now=now-timedelta(minutes=40))
    await scheduler._recover_failed_jobs()
    assert other.calls == 0


@pytest.mark.asyncio
async def test_readiness_route_is_read_only_and_distinct_from_liveness(client):
    response = await client.get("/health/decisions")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert (await client.get("/health")).json()["status"] == "ok"


@pytest.mark.asyncio
async def test_other_job_failures_surface_until_success_without_expanding_retries(engine):
    now = datetime.now(UTC)
    for name in RECOVERABLE_JOBS:
        await receipt(name, now=now, status="ok", error=None)
    await receipt("predictions_evaluator", now=now, status="ok", error=None,
                  summary='{"errors": ["timeout: secret-credential-example"]}')
    async with db_mod.get_session() as session:
        state = await collect_decision_readiness(session, user_id="ariel", now=now)
    assert state["status"] == "degraded"
    job = next(j for j in state["jobs"] if j["name"] == "predictions_evaluator")
    assert job["failure"]["code"] == "timeout"
    assert "secret-credential-example" not in str(state)
    assert "predictions_evaluator" not in RECOVERABLE_JOBS
    await receipt("predictions_evaluator", now=now, status="ok", error=None)
    async with db_mod.get_session() as session:
        state = await collect_decision_readiness(session, user_id="ariel", now=now)
    assert state["status"] == "ready"


@pytest.mark.parametrize("error,code", [
    ("authentication_error", "authentication"), ("OAuth token expired", "authentication"),
    ("monthly budget exceeded", "budget"), ("rate limit exceeded", "rate_limit"),
    ("something unexpected", "job_failed"),
    ("curl: (6) Could not resolve host: finance.yahoo.com", "network_dns"),
    ("[Errno 11001] getaddrinfo failed", "network_dns"),
    ("Temporary failure in name resolution", "network_dns"),
    ("[Errno -2] Name or service not known", "network_dns"),
    ("sqlite3.OperationalError: database is locked", "database_busy"),
])
def test_failure_guidance(error, code):
    from argosy.services.decision_readiness import failure_guidance
    assert failure_guidance(SimpleNamespace(error_message=error, output_summary=None))["code"] == code


@pytest.mark.parametrize("error,code", [
    ("smtp_not_configured", "email_not_configured"),
    ("annual: domain_refresh error: Command failed with exit code 1", "knowledge_refresh_runtime"),
])
def test_specific_integration_guidance(error, code):
    from argosy.services.decision_readiness import failure_guidance
    assert failure_guidance(SimpleNamespace(error_message=error, output_summary=None))["code"] == code


@pytest.mark.asyncio
async def test_disabled_discord_is_history_not_an_active_failure(engine, monkeypatch):
    monkeypatch.setattr("argosy.config.get_settings", lambda: SimpleNamespace(discord_listener_enabled=False))
    now = datetime.now(UTC)
    for name in RECOVERABLE_JOBS:
        await receipt(name, now=now, status="ok", error=None)
    await receipt("discord_listener", now=now, status="error", error="no close frame")
    async with db_mod.get_session() as session:
        state = await collect_decision_readiness(session, user_id="ariel", now=now)
    assert state["status"] == "ready"
    discord = next(j for j in state["jobs"] if j["name"] == "discord_listener")
    assert discord["status"] == "disabled"
    assert discord["recorded_status"] == "error"
    assert not discord["attention_required"]
    assert discord["failure"] is None
    monkeypatch.setattr("argosy.config.get_settings", lambda: SimpleNamespace(discord_listener_enabled=True))
    async with db_mod.get_session() as session:
        state = await collect_decision_readiness(session, user_id="ariel", now=now)
    assert state["status"] == "degraded"


@pytest.mark.asyncio
async def test_disabled_email_preserves_history_and_restores_failure_when_enabled(engine, monkeypatch):
    from argosy.agent_settings import AgentSettings
    settings = AgentSettings()
    settings.cadences.weekly_email_digest.enabled = False
    def read_settings(user_id, *, create_if_missing):
        assert create_if_missing is False
        return settings
    monkeypatch.setattr("argosy.agent_settings.load_agent_settings", read_settings)
    now = datetime.now(UTC)
    for name in RECOVERABLE_JOBS:
        await receipt(name, now=now, status="ok", error=None)
    await receipt("weekly_email_digest", now=now, error="smtp_not_configured")
    async with db_mod.get_session() as session:
        state = await collect_decision_readiness(session, user_id="ariel", now=now)
    email = next(j for j in state["jobs"] if j["name"] == "weekly_email_digest")
    assert state["status"] == "ready"
    assert email["status"] == "disabled"
    assert email["recorded_status"] == "error"
    assert not email["attention_required"]
    settings.cadences.weekly_email_digest.enabled = True
    async with db_mod.get_session() as session:
        state = await collect_decision_readiness(session, user_id="ariel", now=now)
    email = next(j for j in state["jobs"] if j["name"] == "weekly_email_digest")
    assert email["failure"]["code"] == "email_not_configured"


def test_read_only_agent_settings_does_not_create_missing_file(tmp_path, monkeypatch):
    from argosy.agent_settings import load_agent_settings
    target = tmp_path / "absent" / "agent_settings.yaml"
    monkeypatch.setattr("argosy.agent_settings.get_settings", lambda: SimpleNamespace(agent_settings_path=lambda user_id: target))
    assert load_agent_settings("ariel", create_if_missing=False).cadences.weekly_email_digest.enabled
    assert not target.parent.exists()


@pytest.mark.asyncio
async def test_annual_retry_does_not_disappear_or_claim_recovery(engine):
    now = datetime.now(UTC)
    for name in RECOVERABLE_JOBS:
        await receipt(name, now=now, status="ok", error=None)
    await receipt("annual", now=now - timedelta(hours=2), error="domain_refresh failed")
    await receipt("annual", now=now, status="running", error=None)
    async with db_mod.get_session() as session:
        state = await collect_decision_readiness(session, user_id="ariel", now=now)
    annual = next(j for j in state["jobs"] if j["name"] == "annual")
    assert annual["status"] == "running"
    assert annual["attention_required"]
    assert state["status"] == "degraded"
