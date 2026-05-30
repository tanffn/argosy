"""Tests for the per-ticker analyst orchestrator (`/consult` fix).

Covers:
  - Happy path: all 6 always-on analysts succeed → reports persisted +
    returned.
  - Empty citations dropped (codex BLOCKER #3 — agent contract).
  - Quorum total: < 3 succeeded → InsufficientAnalystQuorum.
  - Quorum ticker-specific: 0 ticker-specific roles → InsufficientAnalystQuorum.
  - Failures don't break the run (one analyst exception, others continue).
  - `open_decision_run_for_consult` writes the row.

All 6 per-analyst runners + the gather bundle are monkeypatched so the
test runs offline + deterministic. No real LLM calls.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from argosy.agents.base import AgentReport, ConfidenceBand
from argosy.decisions import per_ticker_analysts as pta
from argosy.decisions.per_ticker_analysts import (
    InsufficientAnalystQuorum,
    open_decision_run_for_consult,
    run_per_ticker_analysts,
)
from argosy.state import db as db_mod
from argosy.state.models import AgentReport as AgentReportRow, DecisionRun, User


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


class _CannedOutput(BaseModel):
    """Minimal pydantic stand-in for an analyst's structured output."""

    cited_sources: list[str] = []
    note: str = ""


def _canned_report(role: str, *, cited: list[str] | None = None) -> AgentReport:
    """Build an AgentReport with the given role + citations.

    Passing ``cited=[]`` produces an empty-citations report (which the
    orchestrator should DROP per codex BLOCKER #3).
    """
    return AgentReport(
        agent_role=role,
        user_id="ariel",
        model="claude-sonnet-4-6",
        response_text="{}",
        tokens_in=10,
        tokens_out=10,
        cost_usd=0.0,
        prompt_hash="hash",
        confidence=ConfidenceBand.MEDIUM,
        output=_CannedOutput(cited_sources=cited or []),
    )


async def _seed_user() -> None:
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()


def _patch_gathers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the real network/sqlite gather; return empty payloads.

    The analyst runners we patch below take payloads but ignore them in
    tests — we only care about WHICH analysts succeed/fail.
    """
    async def _stub_gather(**_kwargs: Any) -> dict[str, Any]:
        return {
            "fundamentals": {},
            "news": {},
            "indicators": {},
            "social": {},
            "macro": {},
            "fx": {},
        }
    monkeypatch.setattr(
        pta, "_gather_inputs_for_ticker", _stub_gather,
    )


def _patch_runners(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail: dict[str, type[BaseException]] | None = None,
    empty_citations: set[str] | None = None,
) -> None:
    """Replace each `_run_*` runner with a deterministic stub.

    ``fail`` maps role → exception class to raise (simulates analyst
    crash). ``empty_citations`` is a set of roles whose stub will
    return an AgentReport with empty cited_sources (should be dropped
    by the orchestrator).

    Roles not in either set succeed with a single canned citation.
    """
    fail = fail or {}
    empty_citations = empty_citations or set()

    def _make_stub(role: str):
        async def _stub(*_args: Any, **_kwargs: Any) -> AgentReport:
            if role in fail:
                raise fail[role](f"stub failure for {role}")
            cited: list[str] = [] if role in empty_citations else [f"{role}:source"]
            return _canned_report(role, cited=cited)
        return _stub

    monkeypatch.setattr(pta, "_run_fundamentals", _make_stub("fundamentals"))
    monkeypatch.setattr(pta, "_run_technical", _make_stub("technical"))
    monkeypatch.setattr(pta, "_run_news", _make_stub("news"))
    monkeypatch.setattr(pta, "_run_sentiment", _make_stub("sentiment"))
    monkeypatch.setattr(pta, "_run_macro", _make_stub("macro"))
    monkeypatch.setattr(pta, "_run_fx", _make_stub("fx"))


# ----------------------------------------------------------------------
# open_decision_run_for_consult
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_decision_run_for_consult_writes_row(engine: None) -> None:
    await _seed_user()
    run_id = await open_decision_run_for_consult(
        user_id="ariel", ticker="XYL", tier_value="T2",
    )
    assert run_id > 0
    async with db_mod.get_session() as session:
        row = await session.get(DecisionRun, run_id)
        assert row is not None
        assert row.user_id == "ariel"
        assert row.ticker == "XYL"
        assert row.tier == "T2"
        assert row.status == "running"


# ----------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_six_succeed_persists_and_returns(
    engine: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_user()
    _patch_gathers(monkeypatch)
    _patch_runners(monkeypatch)

    run_id = await open_decision_run_for_consult(
        user_id="ariel", ticker="XYL", tier_value="T2",
    )
    result = await run_per_ticker_analysts(
        user_id="ariel", ticker="XYL", decision_run_id=run_id,
    )

    assert len(result.reports) == 6
    assert set(result.succeeded_roles) == {
        "fundamentals", "technical", "news", "sentiment", "macro", "fx",
    }
    assert result.skipped_roles == []

    # All six should be persisted under decision_run_id.
    async with db_mod.get_session() as session:
        rows = (await session.execute(
            select(AgentReportRow).where(AgentReportRow.decision_id == str(run_id))
        )).scalars().all()
    assert {r.agent_role for r in rows} == {
        "fundamentals", "technical", "news", "sentiment", "macro", "fx",
    }


# ----------------------------------------------------------------------
# Quorum failures
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quorum_fails_when_only_two_succeed(
    engine: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_user()
    _patch_gathers(monkeypatch)
    _patch_runners(
        monkeypatch,
        fail={
            "technical": RuntimeError,
            "news": RuntimeError,
            "sentiment": RuntimeError,
            "macro": RuntimeError,
        },
    )
    # fundamentals + fx succeed → 2 total, below MIN_QUORUM_TOTAL=3.
    run_id = await open_decision_run_for_consult(
        user_id="ariel", ticker="XYL", tier_value="T2",
    )
    with pytest.raises(InsufficientAnalystQuorum) as exc_info:
        await run_per_ticker_analysts(
            user_id="ariel", ticker="XYL", decision_run_id=run_id,
        )
    assert "fundamentals" in exc_info.value.succeeded
    assert "fx" in exc_info.value.succeeded
    assert len(exc_info.value.succeeded) == 2
    assert len(exc_info.value.failed) == 4


@pytest.mark.asyncio
async def test_quorum_fails_when_no_ticker_specific_succeeds(
    engine: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4 ticker-specific roles all fail → only macro + fx survive.

    Even though we'd hit 2 total + below MIN_QUORUM_TOTAL anyway, this
    test asserts the "ticker-specific hits" branch fires by checking
    the error message mentions it.
    """
    await _seed_user()
    _patch_gathers(monkeypatch)
    _patch_runners(
        monkeypatch,
        fail={
            "fundamentals": RuntimeError,
            "technical": RuntimeError,
            "news": RuntimeError,
            "sentiment": RuntimeError,
        },
    )
    run_id = await open_decision_run_for_consult(
        user_id="ariel", ticker="XYL", tier_value="T2",
    )
    with pytest.raises(InsufficientAnalystQuorum) as exc_info:
        await run_per_ticker_analysts(
            user_id="ariel", ticker="XYL", decision_run_id=run_id,
        )
    # Both quorum-total and ticker-specific paths fail here. The
    # reason string includes "ticker-specific hits: (none)".
    assert "ticker-specific" in exc_info.value.reason


# ----------------------------------------------------------------------
# Failure modes that should still meet quorum
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_three_succeed_one_ticker_specific_meets_quorum(
    engine: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fundamentals + macro + fx succeed; rest fail. 3 total + 1 ticker-specific."""
    await _seed_user()
    _patch_gathers(monkeypatch)
    _patch_runners(
        monkeypatch,
        fail={
            "technical": RuntimeError,
            "news": RuntimeError,
            "sentiment": RuntimeError,
        },
    )
    run_id = await open_decision_run_for_consult(
        user_id="ariel", ticker="XYL", tier_value="T2",
    )
    result = await run_per_ticker_analysts(
        user_id="ariel", ticker="XYL", decision_run_id=run_id,
    )
    assert set(result.succeeded_roles) == {"fundamentals", "macro", "fx"}
    assert {r for r, _ in result.skipped_roles} == {"technical", "news", "sentiment"}
    assert len(result.reports) == 3


# ----------------------------------------------------------------------
# Empty citations are dropped
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_citation_reports_are_dropped(
    engine: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex BLOCKER #3 — empty-cited_sources outputs must not persist."""
    await _seed_user()
    _patch_gathers(monkeypatch)
    _patch_runners(
        monkeypatch,
        # technical comes back with empty citations; should be skipped.
        empty_citations={"technical"},
    )
    run_id = await open_decision_run_for_consult(
        user_id="ariel", ticker="XYL", tier_value="T2",
    )
    result = await run_per_ticker_analysts(
        user_id="ariel", ticker="XYL", decision_run_id=run_id,
    )
    assert "technical" not in result.succeeded_roles
    assert ("technical", "empty_citations") in result.skipped_roles
    assert len(result.reports) == 5

    # Verify no agent_reports row was written for technical.
    async with db_mod.get_session() as session:
        rows = (await session.execute(
            select(AgentReportRow)
            .where(AgentReportRow.decision_id == str(run_id))
            .where(AgentReportRow.agent_role == "technical")
        )).scalars().all()
    assert rows == []


# ----------------------------------------------------------------------
# Single failure doesn't cancel others
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_analyst_exception_does_not_abort_others(
    engine: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_user()
    _patch_gathers(monkeypatch)
    _patch_runners(
        monkeypatch,
        fail={"news": ConnectionError},
    )
    run_id = await open_decision_run_for_consult(
        user_id="ariel", ticker="XYL", tier_value="T2",
    )
    result = await run_per_ticker_analysts(
        user_id="ariel", ticker="XYL", decision_run_id=run_id,
    )
    assert "news" not in result.succeeded_roles
    assert len(result.reports) == 5
    failed_roles = {r for r, _ in result.skipped_roles}
    assert "news" in failed_roles


# ----------------------------------------------------------------------
# close_decision_run_blocked — codex BLOCKER fix on impl review
# (orphan decision_run cleanup when quorum fails).
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_decision_run_blocked_updates_row(engine: None) -> None:
    """The quorum-failure path closes the pre-opened decision_run with
    status='blocked' instead of leaving it orphaned at 'running'."""
    from argosy.decisions.per_ticker_analysts import close_decision_run_blocked

    await _seed_user()
    run_id = await open_decision_run_for_consult(
        user_id="ariel", ticker="XYL", tier_value="T2",
    )
    async with db_mod.get_session() as session:
        row = await session.get(DecisionRun, run_id)
        assert row.status == "running"
        assert row.finished_at is None

    await close_decision_run_blocked(
        decision_run_id=run_id, reason="quorum_failed_test",
    )

    async with db_mod.get_session() as session:
        row = await session.get(DecisionRun, run_id)
        assert row.status == "blocked"
        assert row.finished_at is not None


@pytest.mark.asyncio
async def test_close_decision_run_blocked_is_noop_for_missing_id(
    engine: None,
) -> None:
    """Idempotent / defensive — closing a non-existent id does not raise."""
    from argosy.decisions.per_ticker_analysts import close_decision_run_blocked

    await _seed_user()
    # 99999 doesn't exist; should be a silent no-op.
    await close_decision_run_blocked(
        decision_run_id=99999, reason="missing row test",
    )
