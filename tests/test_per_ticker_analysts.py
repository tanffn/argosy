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
    """Skip the real network/sqlite gather; return NON-empty payloads.

    Each key carries a single sentinel entry so the orchestrator's
    pre-skip-on-empty-payload guard (added 2026-05-30 after the
    live-e2e revealed 4/6 gathers return empty without paid API keys)
    routes every analyst through to its (monkeypatched) runner. The
    runners we patch below ignore payload contents — we only care
    about WHICH analysts succeed/fail in the orchestrator's quorum
    logic.
    """
    async def _stub_gather(**_kwargs: Any) -> dict[str, Any]:
        return {
            "fundamentals": {"XYL": {"pe": 20.0}},
            "news": {"XYL": [{"headline": "test"}]},
            "indicators": {"XYL": {"rsi_14": 55.0}},
            "social": {"XYL": [{"text": "test"}]},
            "macro": {"vix": 15.0},
            "fx": {"USD/NIS": {"latest": 3.7}},
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
async def test_quorum_fails_when_only_one_succeeds(
    engine: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MIN_QUORUM_TOTAL=2; one survivor is below."""
    await _seed_user()
    _patch_gathers(monkeypatch)
    _patch_runners(
        monkeypatch,
        fail={
            "technical": RuntimeError,
            "news": RuntimeError,
            "sentiment": RuntimeError,
            "macro": RuntimeError,
            "fx": RuntimeError,
        },
    )
    # Only fundamentals succeeds → 1 total, below MIN_QUORUM_TOTAL=2.
    run_id = await open_decision_run_for_consult(
        user_id="ariel", ticker="XYL", tier_value="T2",
    )
    with pytest.raises(InsufficientAnalystQuorum) as exc_info:
        await run_per_ticker_analysts(
            user_id="ariel", ticker="XYL", decision_run_id=run_id,
        )
    assert "fundamentals" in exc_info.value.succeeded
    assert len(exc_info.value.succeeded) == 1
    assert len(exc_info.value.failed) == 5


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
async def test_two_succeed_one_ticker_specific_meets_quorum(
    engine: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """technical + fx succeed; rest fail. 2 total + 1 ticker-specific
    (matches the no-data-API-keys dev environment baseline)."""
    await _seed_user()
    _patch_gathers(monkeypatch)
    _patch_runners(
        monkeypatch,
        fail={
            "fundamentals": RuntimeError,
            "news": RuntimeError,
            "sentiment": RuntimeError,
            "macro": RuntimeError,
        },
    )
    run_id = await open_decision_run_for_consult(
        user_id="ariel", ticker="XYL", tier_value="T2",
    )
    result = await run_per_ticker_analysts(
        user_id="ariel", ticker="XYL", decision_run_id=run_id,
    )
    assert set(result.succeeded_roles) == {"technical", "fx"}
    assert {r for r, _ in result.skipped_roles} == {
        "fundamentals", "news", "sentiment", "macro",
    }
    assert len(result.reports) == 2


@pytest.mark.asyncio
async def test_empty_payload_pre_skips_without_llm_call(
    engine: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a gather returns an empty payload, the analyst is
    pre-skipped before any LLM call. Saves cost + gives a clearer
    error than letting the citation gate fire."""
    invoked: dict[str, int] = {r: 0 for r in (
        "fundamentals", "technical", "news", "sentiment", "macro", "fx",
    )}

    # Patch gather to return SOME payloads empty (fundamentals, news,
    # sentiment, macro) and others populated (indicators, fx).
    async def _mixed(**_kwargs: Any) -> dict[str, Any]:
        return {
            "fundamentals": {},  # empty → pre-skip
            "news": {},          # empty → pre-skip
            "indicators": {"XYL": {"rsi_14": 55.0}},
            "social": {},        # empty → pre-skip
            "macro": {},         # empty → pre-skip
            "fx": {"USD/NIS": {"latest": 3.7}},
        }
    monkeypatch.setattr(pta, "_gather_inputs_for_ticker", _mixed)

    def _make_stub(role: str):
        async def _stub(*_args: Any, **_kwargs: Any) -> AgentReport:
            invoked[role] += 1
            return _canned_report(role, cited=[f"{role}:source"])
        return _stub

    monkeypatch.setattr(pta, "_run_fundamentals", _make_stub("fundamentals"))
    monkeypatch.setattr(pta, "_run_technical", _make_stub("technical"))
    monkeypatch.setattr(pta, "_run_news", _make_stub("news"))
    monkeypatch.setattr(pta, "_run_sentiment", _make_stub("sentiment"))
    monkeypatch.setattr(pta, "_run_macro", _make_stub("macro"))
    monkeypatch.setattr(pta, "_run_fx", _make_stub("fx"))

    await _seed_user()
    run_id = await open_decision_run_for_consult(
        user_id="ariel", ticker="XYL", tier_value="T2",
    )
    result = await run_per_ticker_analysts(
        user_id="ariel", ticker="XYL", decision_run_id=run_id,
    )
    # Only technical + fx were invoked (had non-empty payloads).
    assert invoked["technical"] == 1
    assert invoked["fx"] == 1
    assert invoked["fundamentals"] == 0
    assert invoked["news"] == 0
    assert invoked["sentiment"] == 0
    assert invoked["macro"] == 0
    assert set(result.succeeded_roles) == {"technical", "fx"}
    # Empty-payload reasons surface in skipped_roles with the cleaner string.
    reasons = {r: why for r, why in result.skipped_roles}
    assert "empty_payload" in reasons["fundamentals"]
    assert "empty_payload" in reasons["news"]
    assert "empty_payload" in reasons["sentiment"]
    assert "empty_payload" in reasons["macro"]


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


# ----------------------------------------------------------------------
# Long-hold mode — 2026-05-31. Skips technical + fx; runs fundamentals
# + news + sentiment + macro.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_long_hold_mode_skips_technical_and_fx(
    engine: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mode='long_hold' must NOT invoke technical or fx — they're
    irrelevant for multi-year-horizon decisions per
    [[user_long_hold_investor]]."""
    invoked: dict[str, int] = {r: 0 for r in (
        "fundamentals", "technical", "news", "sentiment", "macro", "fx",
    )}

    def _make_stub(role: str):
        async def _stub(*_args: Any, **_kwargs: Any) -> AgentReport:
            invoked[role] += 1
            return _canned_report(role, cited=[f"{role}:source"])
        return _stub

    _patch_gathers(monkeypatch)
    monkeypatch.setattr(pta, "_run_fundamentals", _make_stub("fundamentals"))
    monkeypatch.setattr(pta, "_run_technical", _make_stub("technical"))
    monkeypatch.setattr(pta, "_run_news", _make_stub("news"))
    monkeypatch.setattr(pta, "_run_sentiment", _make_stub("sentiment"))
    monkeypatch.setattr(pta, "_run_macro", _make_stub("macro"))
    monkeypatch.setattr(pta, "_run_fx", _make_stub("fx"))

    await _seed_user()
    run_id = await open_decision_run_for_consult(
        user_id="ariel", ticker="XYL", tier_value="T2",
    )
    result = await run_per_ticker_analysts(
        user_id="ariel", ticker="XYL", decision_run_id=run_id, mode="long_hold",
    )
    # Long-hold set: 4 analysts succeed.
    assert set(result.succeeded_roles) == {"fundamentals", "news", "sentiment", "macro"}
    # Technical + FX should NOT have been invoked at all (not just skipped).
    assert invoked["technical"] == 0
    assert invoked["fx"] == 0
    assert invoked["fundamentals"] == 1
    assert invoked["news"] == 1
    assert invoked["sentiment"] == 1
    assert invoked["macro"] == 1


@pytest.mark.asyncio
async def test_tactical_trade_mode_default_keeps_all_six(
    engine: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mode='tactical_trade' (default) runs the full SDD 6-analyst fleet."""
    invoked: dict[str, int] = {r: 0 for r in (
        "fundamentals", "technical", "news", "sentiment", "macro", "fx",
    )}

    def _make_stub(role: str):
        async def _stub(*_args: Any, **_kwargs: Any) -> AgentReport:
            invoked[role] += 1
            return _canned_report(role, cited=[f"{role}:source"])
        return _stub

    _patch_gathers(monkeypatch)
    monkeypatch.setattr(pta, "_run_fundamentals", _make_stub("fundamentals"))
    monkeypatch.setattr(pta, "_run_technical", _make_stub("technical"))
    monkeypatch.setattr(pta, "_run_news", _make_stub("news"))
    monkeypatch.setattr(pta, "_run_sentiment", _make_stub("sentiment"))
    monkeypatch.setattr(pta, "_run_macro", _make_stub("macro"))
    monkeypatch.setattr(pta, "_run_fx", _make_stub("fx"))

    await _seed_user()
    run_id = await open_decision_run_for_consult(
        user_id="ariel", ticker="XYL", tier_value="T2",
    )
    result = await run_per_ticker_analysts(
        user_id="ariel", ticker="XYL", decision_run_id=run_id,
        # tactical_trade is the default; omit to verify default
    )
    assert len(result.reports) == 6
    assert all(invoked[r] == 1 for r in invoked)


@pytest.mark.asyncio
async def test_unknown_mode_raises(
    engine: None,
) -> None:
    """Bad mode string fails fast with ValueError — caller should know."""
    from argosy.decisions.per_ticker_analysts import (
        InsufficientAnalystQuorum, run_per_ticker_analysts,
    )

    await _seed_user()
    run_id = await open_decision_run_for_consult(
        user_id="ariel", ticker="XYL", tier_value="T2",
    )
    with pytest.raises(ValueError, match="unknown consult mode"):
        await run_per_ticker_analysts(
            user_id="ariel", ticker="XYL", decision_run_id=run_id,
            mode="not_a_mode",  # type: ignore[arg-type]
        )


def test_trader_long_hold_prompt_distinct_from_tactical() -> None:
    """The trader's long_hold SYSTEM prompt explicitly tells the agent
    to ignore MACD/RSI timing and FX hedging; the tactical_trade
    prompt does not. This locks the two prompt variants apart."""
    from argosy.agents.trader import TraderAgent

    agent = TraderAgent(user_id="ariel", tier="T2")

    tactical_sys, _ = agent.build_prompt(
        analyst_reports=[],
        debate_outcome={},
        positions_snapshot="",
        user_constraints="",
        ticker="XYL",
        mode="tactical_trade",
    )
    long_hold_sys, _ = agent.build_prompt(
        analyst_reports=[],
        debate_outcome={},
        positions_snapshot="",
        user_constraints="",
        ticker="XYL",
        mode="long_hold",
    )

    assert tactical_sys != long_hold_sys
    # Long-hold prompt must explicitly de-emphasise the things the user
    # called out (MACD, RSI, FX hedging).
    long_hold_lower = long_hold_sys.lower()
    assert "macd" in long_hold_lower
    assert "do not gate on chart timing" in long_hold_lower
    assert "do not cite fx" in long_hold_lower
    # Tactical prompt should NOT contain those de-emphasis instructions.
    assert "do not gate on chart timing" not in tactical_sys.lower()


def test_trader_never_recommend_refresh_in_both_modes() -> None:
    """Codex BLOCKER 2026-05-31 — the 'NEVER RECOMMEND AGENT REFRESHES'
    rule per [[feedback_agents_talk_to_each_other]] must be in BOTH
    trader prompt variants. Without it, the tactical_trade trader
    could still regress to 'recommend agent X re-pull Y' prose that
    punts to the user."""
    from argosy.agents.trader import TraderAgent

    agent = TraderAgent(user_id="ariel", tier="T2")
    for mode in ("tactical_trade", "long_hold"):
        sys_prompt, _ = agent.build_prompt(
            analyst_reports=[],
            debate_outcome={},
            positions_snapshot="",
            user_constraints="",
            ticker="XYL",
            mode=mode,
        )
        assert "never recommend agent refreshes" in sys_prompt.lower(), (
            f"trader prompt for mode={mode!r} missing the agent-refresh "
            "prohibition"
        )
