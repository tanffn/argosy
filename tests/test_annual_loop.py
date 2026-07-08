"""AnnualLoop tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from argosy.agents.base import ModelCall
from argosy.agents.domain_refresh import DomainRefreshAgent
from argosy.api import events
from argosy.orchestrator.cost_guard import reset_cost_guard
from argosy.orchestrator.loops.annual import AnnualLoop
from argosy.orchestrator.loops.base import LoopSchedule
from argosy.state import db as db_mod
from argosy.state.models import AuditLog, PensionFundSnapshot, User

_REFRESH_CANNED = {
    "per_file": [
        {
            "path": "domain_knowledge/tax/israel/capital_gains.md",
            "status": "no_change",
            "diff": None,
            "evidence": [
                {
                    "url": "https://taxes.gov.il/",
                    "retrieved_at": "2026-01-02",
                    "excerpt": "25%.",
                    "tier": 1,
                }
            ],
            "next_refresh_due": "2026-04-02",
            "note": "verified",
        }
    ],
    "summary": "1 file checked.",
    "confidence": "HIGH",
    "cited_sources": ["https://taxes.gov.il/"],
}


def _mock_refresh_factory():
    class _M(DomainRefreshAgent):
        async def _call_model(self, *, system: str, user: str, **_extra: Any) -> ModelCall:
            return ModelCall(
                text=json.dumps(_REFRESH_CANNED),
                tokens_in=200,
                tokens_out=300,
                model=self.model,
            )
    return _M(user_id="ariel")


@pytest.mark.asyncio
async def test_annual_emits_prompts_and_runs_refresh(
    engine: None, tmp_path
) -> None:
    events._reset_for_tests()
    reset_cost_guard()

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    sub_ctx = events.subscribe()
    q = await sub_ctx.__aenter__()

    loop = AnnualLoop(
        schedule=LoopSchedule(cron="0 8 2 1 *"),
        user_id="ariel",
        domain_refresh_factory=_mock_refresh_factory,
        domain_files_provider=lambda: [
            {
                "path": "domain_knowledge/tax/israel/capital_gains.md",
                "frontmatter": "next_refresh_due: 2026-04-01",
                "content": "Capital gains 25%.",
            }
        ],
        # Isolate write-back from the real domain_knowledge/ tree.
        domain_knowledge_root=tmp_path / "domain_knowledge",
    )
    await loop.tick()

    received: list[str] = []
    while not q.empty():
        received.append(q.get_nowait())
    await sub_ctx.__aexit__(None, None, None)

    joined = "\n".join(received)
    assert "tax_filing_prep" in joined
    assert "w8ben_refresh" in joined
    assert "insurance_renewal" in joined

    async with db_mod.get_session() as session:
        audits = (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == "annual.completed")
            )
        ).scalars().all()
    assert len(audits) == 1
    assert "files_reviewed" in audits[0].payload_json


# ----------------------------------------------------------------------
# Silent-death fix (verify-run 2026-07-08): a domain_refresh sub-step
# failure must fail the tick LOUD — job run not-ok, health not-green —
# never land a green `annual` run over a dead agent.
# ----------------------------------------------------------------------


def _failing_refresh_factory():
    class _F(DomainRefreshAgent):
        async def run(self, **_kwargs: Any):  # type: ignore[override]
            from argosy.agents.errors import AgentRunError

            raise AgentRunError(
                "domain_refresh: output is missing required citations "
                "(`cited_sources` is empty or absent)"
            )

    return _F(user_id="ariel")


_ONE_FILE = [
    {
        "path": "domain_knowledge/tax/israel/capital_gains.md",
        "frontmatter": "next_refresh_due: 2026-04-01",
        "content": "Capital gains 25%.",
    }
]


@pytest.mark.asyncio
async def test_annual_domain_refresh_failure_fails_tick_loud(engine: None) -> None:
    """AgentRunError in the domain-refresh sub-step → tick raises (so the
    scheduler records error), with the failure summarized in
    last_output_summary and the audit event still written (partial progress)."""
    events._reset_for_tests()
    reset_cost_guard()

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    loop = AnnualLoop(
        schedule=LoopSchedule(cron="0 8 2 1 *"),
        user_id="ariel",
        domain_refresh_factory=_failing_refresh_factory,
        domain_files_provider=lambda: _ONE_FILE,
    )
    with pytest.raises(RuntimeError, match="domain_refresh sub-step failed"):
        await loop.tick()

    summary = loop.last_output_summary
    assert summary is not None
    assert summary["steps"]["domain_refresh"] == "error"
    assert "missing required citations" in summary["domain_refresh_error"]

    # The independent steps still completed — audit event landed.
    async with db_mod.get_session() as session:
        audits = (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == "annual.completed")
            )
        ).scalars().all()
    assert len(audits) == 1


@pytest.mark.asyncio
async def test_annual_domain_refresh_failure_lands_error_job_run(engine: None) -> None:
    """Through the registry seam: the `annual` job run must record
    status='error' with the sub-step failure in output_summary, and the
    /api/jobs health derivation must surface red (not green)."""
    from argosy.services.jobs import JobMetadata, JobRegistry, RegisteredScheduler
    from argosy.state.models import JobRun

    events._reset_for_tests()
    reset_cost_guard()

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    loop = AnnualLoop(
        schedule=LoopSchedule(cron="0 8 2 1 *"),
        user_id="ariel",
        domain_refresh_factory=_failing_refresh_factory,
        domain_files_provider=lambda: _ONE_FILE,
    )

    class _DummySettings:
        pass

    registry = JobRegistry()
    scheduler = RegisteredScheduler(
        user_id="ariel", settings=_DummySettings(), registry=registry
    )
    registry.bind_scheduler(scheduler)
    scheduler.register_loop(loop)
    registry.register(
        job=loop,
        metadata=JobMetadata(
            name="annual",
            schedule_cron="0 8 2 1 *",
            schedule_human="cron 0 8 2 1 *",
            source_kind="maintenance",
            description="test annual",
            long_running=False,
        ),
    )

    with pytest.raises(RuntimeError, match="domain_refresh sub-step failed"):
        await registry.fire_now("annual", triggered_by="test")

    async with db_mod.get_session() as session:
        row = (
            await session.execute(
                select(JobRun).where(JobRun.job_name == "annual")
            )
        ).scalar_one()
    assert row.status == "error"
    assert "domain_refresh sub-step failed" in (row.error_message or "")
    # Partial progress captured on the exception path.
    assert row.output_summary is not None
    assert "missing required citations" in row.output_summary

    view = await registry.get("annual")
    assert view.last_run_status == "error"
    assert view.health == "red"


@pytest.mark.asyncio
async def test_annual_success_records_step_summary(
    engine: None, tmp_path
) -> None:
    """Green path: tick returns the per-step summary dict (persisted as
    job_runs.output_summary by the registry seam)."""
    events._reset_for_tests()
    reset_cost_guard()

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    loop = AnnualLoop(
        schedule=LoopSchedule(cron="0 8 2 1 *"),
        user_id="ariel",
        domain_refresh_factory=_mock_refresh_factory,
        domain_files_provider=lambda: _ONE_FILE,
        domain_knowledge_root=tmp_path / "domain_knowledge",
    )
    summary = await loop.tick()
    assert summary is not None
    assert summary["steps"]["domain_refresh"] == "ok"
    assert summary["domain_refresh_error"] is None
    assert summary["refresh_summary"] == "1 file checked."


@pytest.mark.asyncio
async def test_annual_with_no_files_still_records_audit(engine: None) -> None:
    events._reset_for_tests()
    reset_cost_guard()

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    loop = AnnualLoop(
        schedule=LoopSchedule(cron="0 8 2 1 *"),
        user_id="ariel",
        domain_refresh_factory=_mock_refresh_factory,
        domain_files_provider=lambda: [],
    )
    await loop.tick()

    async with db_mod.get_session() as session:
        audits = (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == "annual.completed")
            )
        ).scalars().all()
    assert len(audits) == 1


# ----------------------------------------------------------------------
# pension_refresh_callable wiring (Phase 3 follow-up)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_annual_invokes_pension_refresh_once_per_tick(engine: None) -> None:
    """The annual-loop instance is per-user, so a tick calls the
    pension-refresh callable exactly once with the loop's `user_id`."""
    events._reset_for_tests()
    reset_cost_guard()

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    invocations: list[str] = []

    def _refresh(user_id: str) -> int:
        invocations.append(user_id)
        return 3  # arbitrary "3 funds refreshed" return

    loop = AnnualLoop(
        schedule=LoopSchedule(cron="0 8 2 1 *"),
        user_id="ariel",
        domain_refresh_factory=_mock_refresh_factory,
        domain_files_provider=lambda: [],
        pension_refresh_callable=_refresh,
    )
    await loop.tick()

    assert invocations == ["ariel"]

    async with db_mod.get_session() as session:
        audits = (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == "annual.completed")
            )
        ).scalars().all()
    assert len(audits) == 1
    assert "pensions_refreshed" in audits[0].payload_json
    assert '"pensions_refreshed": 3' in audits[0].payload_json


@pytest.mark.asyncio
async def test_annual_pension_refresh_failure_swallowed_within_tick(
    engine: None,
) -> None:
    """A pension-refresh exception MUST be swallowed by the per-user
    loop tick — the rest of the annual loop (audit log write, domain
    refresh prompts) must still complete cleanly.

    AnnualLoop is per-user, so this test exercises a single tick:
    multi-user isolation falls out of the per-user-instance design and
    doesn't need its own assertion."""
    events._reset_for_tests()
    reset_cost_guard()

    async with db_mod.get_session() as session:
        session.add(User(id="user_a"))
        await session.commit()

    a_invocations: list[str] = []

    def _refresh_a(user_id: str) -> int:
        a_invocations.append(user_id)
        raise RuntimeError("user_a's gemelnet snapshot blew up")

    loop_a = AnnualLoop(
        schedule=LoopSchedule(cron="0 8 2 1 *"),
        user_id="user_a",
        domain_refresh_factory=_mock_refresh_factory,
        domain_files_provider=lambda: [],
        pension_refresh_callable=_refresh_a,
    )

    # The tick must not raise — the loop swallows pension-refresh
    # exceptions defensively so the rest of the annual flow completes.
    await loop_a.tick()

    assert a_invocations == ["user_a"]

    async with db_mod.get_session() as session:
        audits = (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == "annual.completed")
            )
        ).scalars().all()
    by_user = {a.user_id: a for a in audits}
    assert "user_a" in by_user, "user_a's audit should still land despite exception"


@pytest.mark.asyncio
async def test_annual_pension_refresh_persists_snapshot_end_to_end(
    engine: None,
) -> None:
    """A pension-refresh callable that uses ``persist_pension_snapshot``
    must successfully write a `pension_fund_snapshots` row through the
    in-memory test DB. Stubs the gemelnet adapter so no network call
    happens; asserts the row landed."""
    from argosy.adapters.data.gemelnet_adapter import persist_pension_snapshot

    events._reset_for_tests()
    reset_cost_guard()

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    # Canned "adapter" output — what GemelnetAdapter.get_fund_returns
    # would have produced. We hand-build it to avoid coupling this test
    # to the parser.
    canned_returns = {
        "fund_id": "1234",
        "period": "12m",
        "return_pct": 11.5,
        "benchmark_return_pct": 9.0,
        "relative_to_benchmark_pct": 2.5,
        "last_updated": "2026-04-01",
        "source_url": "http://gemelnet.mof.gov.il/Tsuot/UI/DafMakdim.aspx",
        "fund_name": "Stub Hishtalmut",
        "fund_type": "keren_hishtalmut",
        "manager": "Stub Manager",
    }

    async def _refresh(user_id: str) -> int:
        await persist_pension_snapshot(
            user_id=user_id,
            fund_returns=canned_returns,
            balance_nis=50000,
            snapshot_at=datetime.now(UTC),
        )
        return 1

    loop = AnnualLoop(
        schedule=LoopSchedule(cron="0 8 2 1 *"),
        user_id="ariel",
        domain_refresh_factory=_mock_refresh_factory,
        domain_files_provider=lambda: [],
        pension_refresh_callable=_refresh,
    )
    await loop.tick()

    async with db_mod.get_session() as session:
        snaps = (
            await session.execute(
                select(PensionFundSnapshot).where(
                    PensionFundSnapshot.user_id == "ariel"
                )
            )
        ).scalars().all()
    assert len(snaps) == 1
    assert snaps[0].fund_id == "1234"
    assert snaps[0].fund_type == "keren_hishtalmut"
    assert float(snaps[0].balance_nis) == pytest.approx(50000)


@pytest.mark.asyncio
async def test_annual_without_pension_callable_is_a_noop(engine: None) -> None:
    """When `pension_refresh_callable` is omitted, the loop must complete
    without touching pensions and record `pensions_refreshed: null`."""
    events._reset_for_tests()
    reset_cost_guard()

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    loop = AnnualLoop(
        schedule=LoopSchedule(cron="0 8 2 1 *"),
        user_id="ariel",
        domain_refresh_factory=_mock_refresh_factory,
        domain_files_provider=lambda: [],
        # pension_refresh_callable intentionally omitted.
    )
    await loop.tick()

    async with db_mod.get_session() as session:
        audits = (
            await session.execute(
                select(AuditLog).where(AuditLog.event_type == "annual.completed")
            )
        ).scalars().all()
    assert len(audits) == 1
    assert '"pensions_refreshed": null' in audits[0].payload_json
