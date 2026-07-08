"""Funnel stage-3 estate/us-situs fixes (verify-run 2026-07-08, SOFI).

Two halves, per doctrine:

* INPUTS — the stage-3 fleet packet must carry the estate/us-situs
  domain_knowledge (the FM previously noted "no domain_knowledge file
  authorizing a US-estate rule was supplied" and routed the question
  forward instead of blocking).
* FLOOR — the deterministic estate rule (``plan_risk_kernel.evaluate_us_situs``,
  the same module that guards deploy) gets ONE more call site, HORIZON-SCOPED
  per Ariel's 2026-07-08 policy refinement: a fleet-approved funnel BUY of a
  US-domiciled non-NVDA instrument attributed to the LONG-HORIZON CORE is
  blocked (fail closed on unknown domicile); a buy attributed to a BOUNDED
  tactical/discovery sleeve proceeds with the estate exposure ANNOTATED on the
  proposal (never hidden), and unknown domicile is flagged for curation.
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
# FLOOR — deterministic estate gate on funnel-approved buys (horizon-scoped)
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


def _force_scope(monkeypatch, scope: str, detail: str) -> None:
    async def _scope(**kwargs):
        return (scope, detail)

    monkeypatch.setattr(dd_mod, "_floor_scope", _scope)


def _plan_doc_json() -> str:
    """A minimal canonical allocation doc: one long-horizon CORE class
    (SCHD) and one bounded high-growth SLEEVE class (IONQ)."""
    from argosy.services.target_allocation_doc import (
        AllocationClassDoc,
        AllocationInstrument,
        TargetAllocationDoc,
    )

    return TargetAllocationDoc(
        anchor_sigma=0.16, blended_sigma=0.17, nvda_cap_pct=13.0, fi_pct=8.0,
        provenance="test",
        classes=[
            AllocationClassDoc(
                label="US broad-market core", snapshot_category="Core Equity",
                sigma_class="us_core", target_pct=60.0,
                instruments=[AllocationInstrument(
                    symbol="SCHD", role="primary",
                    weight_within_class_pct=100.0, domicile="US",
                )],
            ),
            AllocationClassDoc(
                label="x10 moonshot sleeve", snapshot_category="Growth",
                sigma_class="high_growth_basket", target_pct=40.0,
                instruments=[AllocationInstrument(
                    symbol="IONQ", role="primary",
                    weight_within_class_pct=100.0, domicile="US",
                )],
            ),
        ],
        glide=[],
    ).model_dump_json()


@pytest.mark.asyncio
async def test_floor_scope_attributes_core_sleeve_and_discovery(engine: None) -> None:
    """Attribution reads the canonical plan doc: a non-exempt-class
    instrument is CORE; a high_growth_basket-class instrument is SLEEVE
    (by sleeve membership, never ticker); a name in neither is SLEEVE
    (funnel discovery names are sleeve-bounded by construction)."""
    from argosy.state.models import PlanVersion

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        session.add(PlanVersion(
            user_id="ariel", role="current",
            target_allocation_json=_plan_doc_json(),
        ))
        await session.commit()

    scope, detail = await dd_mod._floor_scope(ticker="SCHD", user_id="ariel")
    assert scope == "core"
    assert "US broad-market core" in detail
    scope, detail = await dd_mod._floor_scope(ticker="IONQ", user_id="ariel")
    assert scope == "sleeve"
    assert "x10 moonshot sleeve" in detail
    scope, detail = await dd_mod._floor_scope(ticker="SOFI", user_id="ariel")
    assert scope == "sleeve"


@pytest.mark.asyncio
async def test_floor_scope_defaults_to_sleeve_without_a_plan(engine: None) -> None:
    """No current plan → attribution unavailable → default to the
    sleeve-lenient scope (annotated, never hidden), stated in the detail."""
    scope, detail = await dd_mod._floor_scope(ticker="SOFI", user_id="ariel")
    assert scope == "sleeve"
    assert "attribution unavailable" in detail


@pytest.mark.asyncio
async def test_floor_blocks_core_class_us_buy(monkeypatch) -> None:
    """A LONG-HORIZON CORE buy of a US-domiciled non-NVDA name keeps the
    strict floor: blocked."""
    _force_scope(monkeypatch, "core", "instrument of long-horizon plan core class 'US broad-market core'")
    out = await _apply_us_situs_floor(_approved("SCHD"), user_id="ariel")
    assert out is not None
    assert out.status == "blocked"
    assert out.blocked_by == "us_situs_floor"
    assert "US-situs" in (out.blocked_reason or "")
    assert "long-horizon core buy" in (out.blocked_reason or "")


@pytest.mark.asyncio
async def test_floor_fails_closed_on_uncurated_core_symbol(monkeypatch) -> None:
    """Unknown domicile on a CORE buy = treated US-situs conservatively
    (same rule-module semantics as the plan invariant gate)."""
    _force_scope(monkeypatch, "core", "instrument of long-horizon plan core class 'X'")
    out = await _apply_us_situs_floor(_approved("ZZZUNCURATED"), user_id="ariel")
    assert out is not None
    assert out.status == "blocked"
    assert out.blocked_by == "us_situs_floor"
    assert "instrument_reference" in (out.blocked_reason or "")


@pytest.mark.asyncio
async def test_floor_sleeve_us_buy_passes_with_annotation(engine: None) -> None:
    """A BOUNDED-SLEEVE buy of a US-domiciled name is NOT blocked — it
    proceeds with the estate exposure annotated on the proposal (rationale +
    history), status unchanged."""
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        run = DecisionRun(user_id="ariel", ticker="SOFI", tier="T2", status="approved")
        session.add(run)
        await session.flush()
        prop = ProposalRow(
            user_id="ariel", ticker="SOFI", action="buy", tier="T2",
            status="awaiting_human", decision_run_id=run.id,
            rationale_summary="fleet rationale",
        )
        session.add(prop)
        await session.commit()
        run_id, prop_id = run.id, prop.id

    # No current plan in this DB → attribution defaults to the bounded-sleeve
    # scope (funnel stage-3 buys are sleeve-bounded by construction).
    out = await _apply_us_situs_floor(
        _approved("SOFI", proposal_id=prop_id, decision_run_id=run_id),
        user_id="ariel",
    )
    assert out is None  # buy proceeds

    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, prop_id)
        assert row.status == "awaiting_human"  # unchanged
        assert "fleet rationale" in row.rationale_summary
        assert "US-situs" in row.rationale_summary
        assert "estate exposure accepted for bounded sleeve" in row.rationale_summary
        run_row = await session.get(DecisionRun, run_id)
        assert run_row.status == "approved"  # unchanged
        hist = (
            await session.execute(
                select(ProposalHistory).where(ProposalHistory.proposal_id == prop_id)
            )
        ).scalars().all()
        assert any(
            h.transitioned_by == "us_situs_floor"
            and h.status == "awaiting_human"
            and "estate exposure accepted" in (h.note or "")
            for h in hist
        )


@pytest.mark.asyncio
async def test_floor_sleeve_unknown_domicile_passes_annotated_and_flagged(
    engine: None,
) -> None:
    """Unknown domicile on a SLEEVE buy: proceed, annotate as unknown, and
    flag for instrument_reference curation — never block."""
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        prop = ProposalRow(
            user_id="ariel", ticker="ZZZUNCURATED", action="buy", tier="T2",
            status="awaiting_human",
        )
        session.add(prop)
        await session.commit()
        prop_id = prop.id

    out = await _apply_us_situs_floor(
        _approved("ZZZUNCURATED", proposal_id=prop_id), user_id="ariel"
    )
    assert out is None

    async with db_mod.get_session() as session:
        row = await session.get(ProposalRow, prop_id)
        assert row.status == "awaiting_human"
        assert "domicile UNKNOWN" in row.rationale_summary
        assert "CURATION FLAG" in row.rationale_summary
        assert "instrument_reference" in row.rationale_summary


@pytest.mark.asyncio
async def test_floor_passes_sanctioned_nvda_and_ucits_buys() -> None:
    assert await _apply_us_situs_floor(_approved("NVDA"), user_id="ariel") is None
    # FWRA is UCITS (estate-safe) in the curated reference.
    assert await _apply_us_situs_floor(_approved("FWRA"), user_id="ariel") is None


@pytest.mark.asyncio
async def test_floor_ignores_sells() -> None:
    """The floor gates NEW flows only — a SELL of a US name is fine."""
    assert await _apply_us_situs_floor(
        _approved("SOFI", action="sell"), user_id="ariel"
    ) is None


@pytest.mark.asyncio
async def test_floor_flips_persisted_core_proposal_to_blocked(
    engine: None, monkeypatch,
) -> None:
    """The already-persisted green_light CORE proposal must not stay
    client-visible: the floor flips it to blocked + records the reason in
    ProposalHistory and on the decision run."""
    _force_scope(monkeypatch, "core", "instrument of long-horizon plan core class 'X'")
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
        _approved("SOFI", proposal_id=prop_id, decision_run_id=run_id),
        user_id="ariel",
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


def _patch_flow(monkeypatch) -> None:
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


@pytest.mark.asyncio
async def test_run_deep_decision_applies_floor_to_core_green_light(monkeypatch) -> None:
    """End-to-end through run_deep_decision: a fleet green_light CORE BUY of
    a US-domiciled non-NVDA name comes back blocked_by='us_situs_floor'."""
    _patch_flow(monkeypatch)
    _force_scope(monkeypatch, "core", "instrument of long-horizon plan core class 'X'")

    out = await run_deep_decision(user_id="ariel", ticker="SOFI")
    assert out.status == "blocked"
    assert out.blocked_by == "us_situs_floor"
    assert "US-situs" in (out.blocked_reason or "")


@pytest.mark.asyncio
async def test_run_deep_decision_sleeve_green_light_proceeds(monkeypatch) -> None:
    """End-to-end: a fleet green_light BOUNDED-SLEEVE BUY of a US-domiciled
    name is approved (annotated), NOT blocked."""
    _patch_flow(monkeypatch)
    _force_scope(monkeypatch, "sleeve", "bounded single-name/discovery position")

    out = await run_deep_decision(user_id="ariel", ticker="SOFI")
    assert out.status == "approved"
    assert out.blocked_by is None
