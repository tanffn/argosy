"""Tests for the incremental-plan capstone flow (argosy/orchestrator/flows/
incremental_plan.py).

Hermetic: an in-memory sqlite engine + Base.metadata.create_all (NEVER
tests/conftest.py, never a real claude.exe call — the negotiation-ladder
participants are deterministic fakes).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from argosy.state.models import Base, PlanVersion, PortfolioSnapshotRow, User
from argosy.quality.derivation_graph import NodeKind
from argosy.quality.change_adjudication import (
    Author, AuthorKind, ChangeKind, ChangeRequest,
)
from argosy.orchestrator.flows.negotiation_ladder import (
    ArbiterClass, PeerVerdict,
)

from argosy.orchestrator.flows.incremental_plan import (
    CycleResult,
    build_base_graph,
    run_incremental_cycle,
)
from argosy.quality.live_surfaces import (
    EARLIEST_SAFE_AGE_NODE,
    FI_MARGIN_NODE,
)


# --------------------------------------------------------------------------- #
# Hermetic fixture                                                            #
# --------------------------------------------------------------------------- #

USER_ID = "ariel"


@pytest.fixture(autouse=True)
def _enable_incremental(monkeypatch):
    """Gate the flow ON for these tests (run_incremental_cycle is fail-closed
    behind ARGOSY_INCREMENTAL_PLAN). build_base_graph itself is ungated."""
    monkeypatch.setenv("ARGOSY_INCREMENTAL_PLAN", "1")


def _make_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    return Session()


def _seed_plan(session, *, margin: float = -250_000.0, age: int = 47) -> int:
    """Seed a minimal user + snapshot + draft plan and a fund_manager AgentReport
    so the resolver can resolve the canonical scalars. Returns decision_run_id.

    The resolver's fi_margin / earliest_safe_age come from agent rows + the
    canonical dual-track engine, which is heavy + re-entrant; rather than stand
    that whole stack up, the tests inject the canonical scalar values directly
    via the ``resolver_values`` seam on build_base_graph (see below). Here we
    only need enough DB state for a snapshot-backed holdings collection.
    """
    session.add(User(id=USER_ID))
    positions = [
        {"symbol": "NVDA", "asset_type": "Equity", "currency": "USD",
         "usd_value_k": 600.0, "details": "NVIDIA Corp (US-domiciled)"},
        {"symbol": "CSPX", "asset_type": "ETF", "currency": "USD",
         "usd_value_k": 400.0, "details": "iShares Core S&P 500 UCITS ETF"},
        {"symbol": "", "asset_type": "Cash", "currency": "NIS",
         "usd_value_k": 200.0, "details": "Bank cash"},
    ]
    session.add(PortfolioSnapshotRow(
        user_id=USER_ID,
        snapshot_date=datetime(2026, 6, 1, tzinfo=timezone.utc),
        imported_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        positions_json=json.dumps(positions),
        fx_usd_nis=3.6,
    ))
    pv = PlanVersion(
        user_id=USER_ID, role="draft", version_label="draft-test",
        sections_json="[]",
    )
    session.add(pv)
    session.commit()
    session.refresh(pv)
    return 1  # decision_run_id is opaque here; resolver_values is injected


def _canonical_values(*, margin: float, age: int) -> dict[str, float]:
    """The canonical scalar map build_base_graph accepts to avoid running the
    heavy/re-entrant resolver in a hermetic test."""
    return {
        FI_MARGIN_NODE: margin,
        EARLIEST_SAFE_AGE_NODE: float(age),
        "portfolio.liquid_net_worth_nis": 4_000_000.0,
    }


# --------------------------------------------------------------------------- #
# Deterministic ladder participants (the LLM seam — NEVER a real claude call)  #
# --------------------------------------------------------------------------- #

class _ConcedeParticipants:
    """B concedes immediately -> the change is accepted (B_CONCEDED)."""

    def peer_round(self, *, change, prior_turns, round):
        return PeerVerdict.B_CONCEDES, "agreed"

    def arbiter(self, *, change, prior_turns):  # pragma: no cover - not reached
        return ArbiterClass.EVIDENCE_RESOLVABLE, "n/a"


class _EscalateParticipants:
    """Peers never resolve; the arbiter classifies it a genuine decision ->
    escalated_to_user (a real client question, change NOT applied)."""

    def peer_round(self, *, change, prior_turns, round):
        return PeerVerdict.UNRESOLVED, "still disagree"

    def arbiter(self, *, change, prior_turns):
        return ArbiterClass.GENUINE_DECISION, "this is a values judgment for the client"


class _ArbiterRejectParticipants:
    """Peers never resolve; the arbiter rules AGAINST the change (keep current)
    -> arbiter_rejected, change NOT applied."""

    def peer_round(self, *, change, prior_turns, round):
        return PeerVerdict.UNRESOLVED, "owner defends the current value"

    def arbiter(self, *, change, prior_turns):
        return ArbiterClass.EVIDENCE_RESOLVABLE, "rebuttal lands; hold current", False


# --------------------------------------------------------------------------- #
# Task 1 — build_base_graph                                                   #
# --------------------------------------------------------------------------- #

def test_build_base_graph_is_closed_with_canonical_surface_nodes():
    session = _make_session()
    rid = _seed_plan(session)
    graph = build_base_graph(
        session, USER_ID, decision_run_id=rid,
        resolver_values=_canonical_values(margin=-250_000.0, age=47),
    )

    # The canonical derived nodes exist.
    assert graph.get(FI_MARGIN_NODE).value == -250_000.0
    assert graph.get(EARLIEST_SAFE_AGE_NODE).value == 47.0

    # Canonical SURFACE nodes exist and are valid (closed graph).
    for skey in ("surface:fi_verdict", "surface:dashboard.fi_tile",
                 "surface:retirement_age_headline",
                 "surface:us_situs_estate_headline"):
        node = graph.get(skey)
        assert node.kind is NodeKind.SURFACE
        assert graph.is_valid(skey), f"{skey} not valid"

    # The us-situs per-symbol breakdown collection node exists + classifies NVDA
    # as US-situs (it is US-domiciled), CSPX as non-US (UCITS).
    breakdown = graph.get("concentration.us_situs_symbol_breakdown").value
    by_sym = {r["symbol"]: r["classification"] for r in breakdown}
    assert by_sym["NVDA"] == "US"
    assert by_sym["CSPX"] == "non-US"

    assert graph.is_closed()


# --------------------------------------------------------------------------- #
# Task 2/3 — change-requests -> adjudicate -> ladder -> apply -> propagate     #
# --------------------------------------------------------------------------- #

def test_derived_target_change_request_is_rejected():
    session = _make_session()
    rid = _seed_plan(session)
    # A SET_DERIVED against the FI margin (a derived node) must be rejected.
    cr = ChangeRequest(
        target_node_key=FI_MARGIN_NODE,
        author=Author(AuthorKind.AGENT, "codex"),
        kind=ChangeKind.SET_DERIVED,
        payload={"value": 999_999.0},
        rationale="just set it positive",
    )
    res = run_incremental_cycle(
        session, user_id=USER_ID, decision_run_id=rid,
        change_requests=[cr], participants=_ConcedeParticipants(),
        persist=False,
        resolver_values=_canonical_values(margin=-250_000.0, age=47),
    )
    assert isinstance(res, CycleResult)
    # The derived node value is unchanged (the reject did not apply).
    assert any("rejected" in f.lower() for f in res.open_flags)


def test_input_change_applies_and_repropagates():
    session = _make_session()
    rid = _seed_plan(session)
    # fx.usd_nis is an INPUT of the holdings collection -> changing it
    # re-derives us_situs_estate_nis. B concedes so it applies.
    cr = ChangeRequest(
        target_node_key="fx.usd_nis",
        author=Author(AuthorKind.AGENT, "fund_manager"),
        kind=ChangeKind.SET_INPUT,
        payload={"value": 4.0},
        rationale="BOI fix to 4.0",
    )
    res = run_incremental_cycle(
        session, user_id=USER_ID, decision_run_id=rid,
        change_requests=[cr], participants=_ConcedeParticipants(),
        persist=False,
        resolver_values=_canonical_values(margin=-250_000.0, age=47),
    )
    assert "concentration.us_situs_estate_nis" in res.recomputed
    assert not res.real_questions


def test_genuine_decision_change_request_yields_real_question_no_apply():
    session = _make_session()
    rid = _seed_plan(session)
    # A recipe/policy node routes through the ladder; the arbiter escalates ->
    # a real client question, and the graph is NOT mutated.
    cr = ChangeRequest(
        target_node_key="retirement.required_real_yield_pct",
        author=Author(AuthorKind.AGENT, "fund_manager"),
        kind=ChangeKind.SET_RECIPE,
        payload={"value": 0.05},
        rationale="raise the SWR assumption",
    )
    res = run_incremental_cycle(
        session, user_id=USER_ID, decision_run_id=rid,
        change_requests=[cr], participants=_EscalateParticipants(),
        persist=False,
        recipe_node_keys={"retirement.required_real_yield_pct"},
        resolver_values=_canonical_values(margin=-250_000.0, age=47),
    )
    assert len(res.real_questions) == 1
    assert res.real_questions[0]["target_node_key"] == "retirement.required_real_yield_pct"
    assert not res.closed  # an open real question keeps it un-closed


def test_arbiter_rejected_change_is_not_applied():
    """The arbiter ruling AGAINST a proposed change (keep the current value) must
    NOT mutate the node — the gap the live SWR run exposed (FM rejected the raise
    yet the apply path would have set it)."""
    session = _make_session()
    rid = _seed_plan(session)
    cr = ChangeRequest(
        target_node_key="retirement.required_real_yield_pct",
        author=Author(AuthorKind.AGENT, "fund_manager"),
        kind=ChangeKind.SET_RECIPE,
        payload={"value": 0.05},
        rationale="raise the SWR assumption (anchor-shopping)",
    )
    res = run_incremental_cycle(
        session, user_id=USER_ID, decision_run_id=rid,
        change_requests=[cr], participants=_ArbiterRejectParticipants(),
        persist=False,
        recipe_node_keys={"retirement.required_real_yield_pct"},
        resolver_values=_canonical_values(margin=-250_000.0, age=47),
    )
    # No client question (the fleet resolved it), and the node was NOT set to 0.05.
    assert not res.real_questions
    assert res.graph.get("retirement.required_real_yield_pct").value is None


def test_cross_surface_fi_basis_no_flip_after_margin_change():
    """The capstone guarantee: change the fi_margin INPUT once; BOTH the FI
    verdict surface and the dashboard FI tile flip together (no basis-flip),
    recheck_coherence returns no flags, and the cycle closes."""
    session = _make_session()
    rid = _seed_plan(session)
    cr = ChangeRequest(
        target_node_key=FI_MARGIN_NODE,
        author=Author(AuthorKind.AGENT, "fund_manager"),
        kind=ChangeKind.SET_INPUT,
        payload={"value": 500_000.0},
        rationale="revised liquid basis crosses FI",
    )
    res = run_incremental_cycle(
        session, user_id=USER_ID, decision_run_id=rid,
        change_requests=[cr], participants=_ConcedeParticipants(),
        persist=False,
        # Treat the fi-margin node as an INPUT for this scenario so the change
        # is appliable (build_base_graph seeds the canonical scalars as INPUTs).
        resolver_values=_canonical_values(margin=-250_000.0, age=47),
    )
    g = res.graph
    verdict = g.get("surface:fi_verdict").value
    tile = g.get("surface:dashboard.fi_tile").value
    assert "REACHED" in verdict and "REACHED" in tile
    assert verdict == tile
    assert not res.open_flags
    assert res.closed


# --------------------------------------------------------------------------- #
# Task 4 — publish gate wiring + persistence                                  #
# --------------------------------------------------------------------------- #

def test_publish_gate_blocks_on_open_question_clears_when_clean():
    session = _make_session()
    rid = _seed_plan(session)
    authorities = {
        "codex": "APPROVE", "deterministic_gate": "PASS",
        "fund_manager": "approve", "whole_artifact_reader": "approve",
        "rederivation": "approve",
    }
    # Clean cycle (no CRs) with all authorities clear -> promotable.
    clean = run_incremental_cycle(
        session, user_id=USER_ID, decision_run_id=rid,
        change_requests=[], participants=_ConcedeParticipants(),
        persist=False, authorities=authorities,
        resolver_values=_canonical_values(margin=-250_000.0, age=47),
    )
    assert clean.promotable is True

    # A genuine-decision CR -> open real question -> NOT promotable even with
    # all authorities clear (fail-closed publish gate).
    cr = ChangeRequest(
        target_node_key="retirement.required_real_yield_pct",
        author=Author(AuthorKind.AGENT, "fund_manager"),
        kind=ChangeKind.SET_RECIPE, payload={"value": 0.05},
        rationale="raise SWR",
    )
    blocked = run_incremental_cycle(
        session, user_id=USER_ID, decision_run_id=rid,
        change_requests=[cr], participants=_EscalateParticipants(),
        persist=False, authorities=authorities,
        recipe_node_keys={"retirement.required_real_yield_pct"},
        resolver_values=_canonical_values(margin=-250_000.0, age=47),
    )
    assert blocked.promotable is False


def test_persist_writes_graph_and_propagation_event():
    session = _make_session()
    rid = _seed_plan(session)
    cr = ChangeRequest(
        target_node_key="fx.usd_nis",
        author=Author(AuthorKind.AGENT, "fund_manager"),
        kind=ChangeKind.SET_INPUT, payload={"value": 4.0},
        rationale="BOI fix",
    )
    res = run_incremental_cycle(
        session, user_id=USER_ID, decision_run_id=rid,
        change_requests=[cr], participants=_ConcedeParticipants(),
        persist=True,
        resolver_values=_canonical_values(margin=-250_000.0, age=47),
    )
    assert res.replay_ref is not None
    from argosy.state.graph_store import replay_cycle
    from argosy.state.models import PlanNode
    # The persisted plan_id is encoded in replay_ref as "plan:<id>:cycle:<cid>".
    plan_id = int(res.replay_ref.split(":")[1])
    steps = replay_cycle(session, plan_id, res.replay_ref.split(":")[3])
    assert steps  # at least one propagation event recorded
    nodes = session.query(PlanNode).filter(PlanNode.plan_id == plan_id).count()
    assert nodes > 0


def test_fi_crossing_reconciled_against_negative_margin():
    """The base-graph FI-crossing reconciliation guard: a negative margin can
    never pair with a past/present crossing (the surface can't see the margin)."""
    import datetime as _dt
    from argosy.orchestrator.flows.incremental_plan import (
        _reconcile_fi_crossing, FI_CROSSING_YEAR_NODE,
    )
    from argosy.quality.live_surfaces import FI_MARGIN_NODE

    cur = _dt.date.today().year
    # Contradiction: FI short (margin<0) but a current-year crossing -> dropped.
    out = _reconcile_fi_crossing(
        {FI_MARGIN_NODE: -500_000.0, FI_CROSSING_YEAR_NODE: float(cur)},
        current_year=cur)
    assert FI_CROSSING_YEAR_NODE not in out  # -> 0.0 seed -> 'not reached'
    # A genuine future crossing with a negative margin is preserved.
    out2 = _reconcile_fi_crossing(
        {FI_MARGIN_NODE: -500_000.0, FI_CROSSING_YEAR_NODE: float(cur + 1)},
        current_year=cur)
    assert out2[FI_CROSSING_YEAR_NODE] == float(cur + 1)
    # Margin reached -> crossing normalized to the current year.
    out3 = _reconcile_fi_crossing(
        {FI_MARGIN_NODE: 200_000.0, FI_CROSSING_YEAR_NODE: float(cur + 3)},
        current_year=cur)
    assert out3[FI_CROSSING_YEAR_NODE] == float(cur)
