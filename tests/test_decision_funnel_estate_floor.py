"""Funnel stage-3 estate/us-situs fixes (verify-run 2026-07-08, SOFI).

Two halves, per doctrine:

* INPUTS — the stage-3 fleet packet must carry the estate/us-situs
  domain_knowledge (the FM previously noted "no domain_knowledge file
  authorizing a US-estate rule was supplied" and routed the question
  forward instead of blocking).
* FLOOR — the deterministic estate rule (``plan_risk_kernel.evaluate_us_situs``,
  the same module that guards deploy) gets ONE more call site: a
  fleet-approved funnel BUY of a US-domiciled non-NVDA instrument is
  flagged/blocked with the reason recorded.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from argosy.decisions.flow import ApprovedProposal
from argosy.services.decision_funnel import deep_decision as dd_mod
from argosy.services.decision_funnel.deep_decision import (
    _apply_us_situs_floor,
    run_deep_decision,
)
from argosy.services.decision_funnel.estate_kb import (
    estate_constraints_block,
    load_estate_kb,
)
from argosy.state import db as db_mod
from argosy.state.models import (
    DecisionRun,
    Proposal as ProposalRow,
    ProposalHistory,
    User,
)


# ---------------------------------------------------------------------------
# INPUTS — estate KB in the stage-3 packet
# ---------------------------------------------------------------------------


def test_load_estate_kb_reads_the_rule_file() -> None:
    kb = load_estate_kb()
    assert "domain_knowledge/tax/us/estate_tax_nonresidents.md" in kb
    assert kb["domain_knowledge/tax/us/estate_tax_nonresidents.md"].strip()


def test_estate_constraints_block_carries_rule_and_file() -> None:
    block = estate_constraints_block("existing constraint text")
    assert "existing constraint text" in block
    # The binding rule summary.
    assert "NON-US person" in block
    assert "NVDA" in block
    # The full KB file, titled with its repo-relative path so agents can cite it.
    assert "domain_knowledge/tax/us/estate_tax_nonresidents.md" in block


@pytest.mark.asyncio
async def test_stage3_packet_contains_estate_kb(monkeypatch) -> None:
    """run_deep_decision must hand the fleet the estate domain_knowledge via
    ``user_constraints`` (trader / risk team / fund manager all read it)."""
    captured: dict = {}

    async def _open(**kwargs):
        return 7

    async def _analysts(**kwargs):
        return SimpleNamespace(reports=[])

    class _FakeFlow:
        def __init__(self, *, user_id):
            self.user_id = user_id

        async def run(self, **kwargs):
            captured.update(kwargs)
            from argosy.decisions.flow import BlockedProposal

            return BlockedProposal(
                reason="test stop", blocked_by="fund_manager", decision_run_id=7
            )

    monkeypatch.setattr(dd_mod, "open_decision_run_for_consult", _open)
    monkeypatch.setattr(dd_mod, "run_per_ticker_analysts", _analysts)
    monkeypatch.setattr(dd_mod, "DecisionFlow", _FakeFlow)

    out = await run_deep_decision(user_id="ariel", ticker="SOFI")
    assert out.status == "blocked"
    constraints = captured["user_constraints"]
    assert "domain_knowledge/tax/us/estate_tax_nonresidents.md" in constraints
    assert "NON-US person" in constraints
    # Caller-supplied constraints are preserved when present.
    captured.clear()
    await run_deep_decision(user_id="ariel", ticker="SOFI", user_constraints="keep me")
    assert captured["user_constraints"].startswith("keep me")


# ---------------------------------------------------------------------------
# FLOOR — deterministic estate gate on funnel-approved buys
# ---------------------------------------------------------------------------


def _approved(ticker: str, action: str = "buy", proposal_id: int = 0,
              decision_run_id: int = 0) -> ApprovedProposal:
    return ApprovedProposal(
        proposal=SimpleNamespace(
            ticker=ticker, action=action, id=proposal_id,
            size_shares_or_currency=1000.0,
        ),
        fund_manager=None, risk_outcome=None, debate_outcome=None,
        decision_run_id=decision_run_id,
    )


@pytest.mark.asyncio
async def test_floor_blocks_us_domiciled_non_nvda_buy() -> None:
    out = await _apply_us_situs_floor(_approved("SOFI"))
    assert out is not None
    assert out.status == "blocked"
    assert out.blocked_by == "us_situs_floor"
    assert "US-situs" in (out.blocked_reason or "")


@pytest.mark.asyncio
async def test_floor_fails_closed_on_uncurated_symbol() -> None:
    """Unknown domicile = treated US-situs conservatively (same rule-module
    semantics as the plan invariant gate)."""
    out = await _apply_us_situs_floor(_approved("ZZZUNCURATED"))
    assert out is not None
    assert out.status == "blocked"
    assert out.blocked_by == "us_situs_floor"
    assert "instrument_reference" in (out.blocked_reason or "")


@pytest.mark.asyncio
async def test_floor_passes_sanctioned_nvda_and_ucits_buys() -> None:
    assert await _apply_us_situs_floor(_approved("NVDA")) is None
    # FWRA is UCITS (estate-safe) in the curated reference.
    assert await _apply_us_situs_floor(_approved("FWRA")) is None


@pytest.mark.asyncio
async def test_floor_ignores_sells() -> None:
    """The floor gates NEW flows only — a SELL of a US name is fine."""
    assert await _apply_us_situs_floor(_approved("SOFI", action="sell")) is None


@pytest.mark.asyncio
async def test_floor_flips_persisted_proposal_to_blocked(engine: None) -> None:
    """The already-persisted green_light proposal must not stay
    client-visible: the floor flips it to blocked + records the reason in
    ProposalHistory and on the decision run."""
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        run = DecisionRun(
            user_id="ariel", ticker="SOFI", tier="T2", status="approved",
        )
        session.add(run)
        await session.flush()
        prop = ProposalRow(
            user_id="ariel", ticker="SOFI", action="buy", tier="T2",
            status="awaiting_human", decision_run_id=run.id,
        )
        session.add(prop)
        await session.commit()
        run_id, prop_id = run.id, prop.id

    out = await _apply_us_situs_floor(
        _approved("SOFI", proposal_id=prop_id, decision_run_id=run_id)
    )
    assert out is not None and out.status == "blocked"

    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, prop_id)
        assert row.status == "blocked"
        run_row = await session.get(DecisionRun, run_id)
        assert run_row.status == "blocked"
        hist = (
            await session.execute(
                select(ProposalHistory).where(
                    ProposalHistory.proposal_id == prop_id
                )
            )
        ).scalars().all()
        assert any(
            h.transitioned_by == "us_situs_floor" and "US-situs" in (h.note or "")
            for h in hist
        )


@pytest.mark.asyncio
async def test_run_deep_decision_applies_floor_to_green_light(monkeypatch) -> None:
    """End-to-end through run_deep_decision: a fleet green_light BUY of a
    US-domiciled non-NVDA name comes back blocked_by='us_situs_floor'."""

    async def _open(**kwargs):
        return 3

    async def _analysts(**kwargs):
        return SimpleNamespace(reports=[])

    class _FakeFlow:
        def __init__(self, *, user_id):
            self.user_id = user_id

        async def run(self, **kwargs):
            return _approved("SOFI", proposal_id=0, decision_run_id=3)

    monkeypatch.setattr(dd_mod, "open_decision_run_for_consult", _open)
    monkeypatch.setattr(dd_mod, "run_per_ticker_analysts", _analysts)
    monkeypatch.setattr(dd_mod, "DecisionFlow", _FakeFlow)

    out = await run_deep_decision(user_id="ariel", ticker="SOFI")
    assert out.status == "blocked"
    assert out.blocked_by == "us_situs_floor"
    assert "US-situs" in (out.blocked_reason or "")
