"""Stage-3 x10 sleeve-mandate injection for DISCOVERY candidates.

Time-machine backtest lesson (tmp/fleet_timemachine/, 2026-07): the long-hold
lens catches pre-momentum monsters WHEN handed the sleeve mandate (bounded
small position, accepted 100% loss, cap-math asymmetry, mandatory exit
discipline) — production discovery adjudicated new names with a generic
packet, so the fleet judged them like core positions.

Covers, per the deterministic-INPUTS doctrine (mirrors estate_kb /
position_context):

* ``build_sleeve_mandate_context`` — the mandate paragraph is PLAN-OWNED
  (read from the current plan's high_growth_basket class rationale +
  instrument meta, never the hardcoded constant when the plan carries one),
  the per-name position bound is DERIVED from the plan's sleeve target x
  instrument weights, the exit-trigger requirement is explicit, and the live
  funding gap (canonical breakdown attribution) travels with it.
* Fallbacks — no current plan / empty rationale / no snapshot degrade
  honestly (standing ``X10_SLEEVE_MANDATE`` constant, budget-envelope line);
  the packet is never silently mandate-free.
* ``run_deep_decision`` wiring — the block lands in ``user_constraints`` for
  ``subject_type="discovery"`` ONLY, coexists with the estate KB block, and a
  loader failure never kills stage 3.
* Orchestrator wiring — ``run_funnel`` hands ``subject_type`` through to the
  deep-decision callable, so a discovery candidate is adjudicated as one.
"""

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from argosy.services.decision_funnel import deep_decision as dd_mod
from argosy.services.decision_funnel.deep_decision import (
    DeepDecisionOutcome,
    run_deep_decision,
)
from argosy.services.decision_funnel.sleeve_mandate import (
    build_sleeve_mandate_context,
    find_x10_sleeve_class,
)
from argosy.services.high_potential_sleeve import X10_SLEEVE_MANDATE
from argosy.services.target_allocation_doc import (
    AllocationClassDoc,
    AllocationInstrument,
    TargetAllocationDoc,
)
from argosy.state.models import Base, PlanVersion, PortfolioSnapshotRow, User

NOW = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)

_PLAN_MANDATE = (
    "Permanent high-growth 'moonshot' sleeve — the x10 ASYMMETRY sleeve "
    "(binding mandate): names that can plausibly 10x in 5-10 years on "
    "cap-math; favors sub-~$20-30B earlier-stage; accepted per-name loss "
    "is 100%."
)


def _doc(*, hg_rationale: str = _PLAN_MANDATE, with_sleeve: bool = True):
    classes = [
        AllocationClassDoc(
            label="US broad-market core",
            snapshot_category="Core Equity",
            sigma_class="us_core_equity",
            target_pct=60.0,
            instruments=[
                AllocationInstrument(
                    symbol="CSPX", role="primary",
                    weight_within_class_pct=100.0, domicile="IE",
                )
            ],
        ),
    ]
    if with_sleeve:
        classes.append(
            AllocationClassDoc(
                label="High-growth / high-potential",
                snapshot_category="Individual Stocks",
                sigma_class="high_growth_basket",
                target_pct=5.0,
                rationale=hg_rationale,
                instruments=[
                    AllocationInstrument(
                        symbol="RXRX", role="primary",
                        weight_within_class_pct=60.0, domicile="US",
                        exit_triggers=["Oncology read-out fails"],
                    ),
                    AllocationInstrument(
                        symbol="ACHR", role="primary",
                        weight_within_class_pct=20.0, domicile="US",
                    ),
                ],
            )
        )
    return TargetAllocationDoc(
        anchor_sigma=0.12, blended_sigma=0.13, nvda_cap_pct=13.0,
        fi_pct=10.0, provenance="test", classes=classes, glide=[],
    )


@pytest.fixture
def sf():
    eng = sa.create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    SF = sessionmaker(bind=eng, expire_on_commit=False)
    with SF() as s:
        s.add(User(id="ariel", plan="free"))
        s.commit()
    return SF


def _seed_plan(sf, doc=None):
    with sf() as s:
        s.add(
            PlanVersion(
                user_id="ariel", role="current", version_label="v-test",
                target_allocation_json=(
                    doc.model_dump_json() if doc is not None else None
                ),
            )
        )
        s.commit()


def _seed_snapshot(sf, positions):
    # Fresh marks (today): the funnel book flows through the conserved
    # current-book accessor, which degrades a hard-stale snapshot to empty.
    with sf() as s:
        s.add(
            PortfolioSnapshotRow(
                user_id="ariel",
                snapshot_date=date.today(),
                imported_at=datetime.now(UTC),
                positions_json=json.dumps(positions),
            )
        )
        s.commit()


_POSITIONS = [
    # RXRX is a plan sleeve instrument → attributes to the sleeve label:
    # 10k of a 1,000k book = 1.0% current vs the 5.0% target.
    {"symbol": "RXRX", "asset_type": "Individual Stocks", "usd_value_k": 10.0},
    {"symbol": "CSPX", "asset_type": "Core Equity", "usd_value_k": 990.0},
]


# --- find_x10_sleeve_class ---------------------------------------------------


def test_find_x10_class_keys_on_sigma_class_not_label():
    doc = _doc()
    cls = find_x10_sleeve_class(doc)
    assert cls is not None and cls.sigma_class == "high_growth_basket"
    assert find_x10_sleeve_class(_doc(with_sleeve=False)) is None
    assert find_x10_sleeve_class(None) is None


# --- block builder: plan-owned mandate + derived bounds + funding gap --------


def test_block_carries_plan_owned_mandate_not_hardcoded_constant(sf):
    _seed_plan(sf, _doc())
    _seed_snapshot(sf, _POSITIONS)
    with sf() as s:
        block = build_sleeve_mandate_context(s, user_id="ariel")
    # The PLAN's rationale is the mandate paragraph, verbatim.
    assert _PLAN_MANDATE in block
    # The hardcoded constant is the FALLBACK only — not used when the plan
    # carries its own sleeve rationale.
    assert X10_SLEEVE_MANDATE not in block
    # Adjudication frame: the plan class + target are named.
    assert "High-growth / high-potential" in block
    assert "5.0%" in block
    assert "NOT the core allocation" in block


def test_position_bound_is_derived_from_plan_numbers(sf):
    _seed_plan(sf, _doc())
    _seed_snapshot(sf, _POSITIONS)
    with sf() as s:
        block = build_sleeve_mandate_context(s, user_id="ariel")
    # 5.0% sleeve x 20-60% instrument weights → ~1.0%-3.0% of book.
    assert "POSITION IS BOUNDED" in block
    assert "~1.0%-3.0% of the tradeable book" in block
    assert "100%" in block  # accepted per-name loss


def test_exit_trigger_required_in_any_green_light(sf):
    _seed_plan(sf, _doc())
    _seed_snapshot(sf, _POSITIONS)
    with sf() as s:
        block = build_sleeve_mandate_context(s, user_id="ariel")
    assert "EXIT TRIGGER REQUIRED" in block
    assert "1 of 2 current sleeve names carry" in block
    assert "INCOMPLETE" in block


def test_funding_gap_from_canonical_breakdown(sf):
    _seed_plan(sf, _doc())
    _seed_snapshot(sf, _POSITIONS)
    with sf() as s:
        block = build_sleeve_mandate_context(s, user_id="ariel")
    # 1.0% current vs 5.0% target on a $1.0M book → 4.0pp / ~$40,000 headroom.
    assert "SLEEVE FUNDING STATUS" in block
    assert "~1.0% of book vs 5.0% target" in block
    assert "UNDER-FUNDED by ~4.0pp" in block
    assert "$40,000" in block


def test_current_instruments_listed_with_asymmetry_rank(sf):
    _seed_plan(sf, _doc())
    _seed_snapshot(sf, _POSITIONS)
    with sf() as s:
        block = build_sleeve_mandate_context(s, user_id="ariel")
    assert "RXRX 60% (exit triggers recorded)" in block
    assert "ACHR 20%" in block


# --- fallbacks: never silently mandate-free ----------------------------------


def test_no_current_plan_falls_back_to_standing_mandate(sf):
    with sf() as s:
        block = build_sleeve_mandate_context(s, user_id="ariel")
    assert X10_SLEEVE_MANDATE in block
    assert "unavailable" in block
    assert "EXIT TRIGGER REQUIRED" in block
    assert "POSITION IS BOUNDED" in block


def test_plan_without_sleeve_class_falls_back(sf):
    _seed_plan(sf, _doc(with_sleeve=False))
    with sf() as s:
        block = build_sleeve_mandate_context(s, user_id="ariel")
    assert X10_SLEEVE_MANDATE in block


def test_empty_rationale_uses_constant_within_plan_frame(sf):
    _seed_plan(sf, _doc(hg_rationale=""))
    _seed_snapshot(sf, _POSITIONS)
    with sf() as s:
        block = build_sleeve_mandate_context(s, user_id="ariel")
    # Plan sleeve exists (frame + derived numbers used) but its rationale is
    # empty → the standing constant fills the mandate paragraph.
    assert X10_SLEEVE_MANDATE in block
    assert "High-growth / high-potential" in block


def test_no_snapshot_degrades_funding_to_budget_envelope(sf):
    _seed_plan(sf, _doc())
    with sf() as s:
        block = build_sleeve_mandate_context(s, user_id="ariel")
    assert _PLAN_MANDATE in block
    assert "budget envelope" in block


# --- run_deep_decision wiring ------------------------------------------------


def _stub_fleet(monkeypatch, captured: dict):
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

    async def _no_pos(**kwargs):
        return ""

    monkeypatch.setattr(dd_mod, "open_decision_run_for_consult", _open)
    monkeypatch.setattr(dd_mod, "run_per_ticker_analysts", _analysts)
    monkeypatch.setattr(dd_mod, "DecisionFlow", _FakeFlow)
    monkeypatch.setattr(dd_mod, "position_context_block", _no_pos)
    # Isolate the pushback gate from the LIVE verdict registry (see the
    # position-context test): a real settled verdict would short-circuit before
    # the stage-3 packet is built. These tests exercise packet building, not the
    # gate — force "not defended" (imported locally, so patch the source).
    import argosy.services.verdict_registry as _vr_mod

    monkeypatch.setattr(
        _vr_mod, "check_pushback_gate",
        lambda *a, **k: SimpleNamespace(defended=False, standing=None, reason=""),
    )


@pytest.mark.asyncio
async def test_discovery_stage3_packet_carries_sleeve_mandate(monkeypatch):
    captured: dict = {}
    _stub_fleet(monkeypatch, captured)

    sentinel = "X10 / HIGH-POTENTIAL SLEEVE MANDATE — DISCOVERY CANDIDATE ..."

    async def _mandate(**kwargs):
        assert kwargs == {"user_id": "ariel"}
        return sentinel

    monkeypatch.setattr(dd_mod, "x10_sleeve_mandate_block", _mandate)

    out = await run_deep_decision(
        user_id="ariel", ticker="RKLB", subject_type="discovery"
    )
    assert out.status == "blocked"
    assert sentinel in captured["user_constraints"]
    # The estate KB block still travels (both INPUTS fixes coexist).
    assert "domain_knowledge/tax/us/estate_tax_nonresidents.md" in (
        captured["user_constraints"]
    )


@pytest.mark.asyncio
async def test_held_name_packet_has_no_sleeve_mandate(monkeypatch):
    captured: dict = {}
    _stub_fleet(monkeypatch, captured)

    async def _mandate(**kwargs):  # must NOT be called for a held name
        raise AssertionError("sleeve mandate must not load for subject_type=holding")

    monkeypatch.setattr(dd_mod, "x10_sleeve_mandate_block", _mandate)

    out = await run_deep_decision(user_id="ariel", ticker="SOFI")
    assert out.status == "blocked"
    assert "SLEEVE MANDATE" not in captured["user_constraints"]


@pytest.mark.asyncio
async def test_mandate_load_failure_never_kills_stage3(monkeypatch):
    captured: dict = {}
    _stub_fleet(monkeypatch, captured)

    async def _boom(**kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(dd_mod, "x10_sleeve_mandate_block", _boom)

    out = await run_deep_decision(
        user_id="ariel", ticker="RKLB", subject_type="discovery"
    )
    assert out.status == "blocked"  # the flow still ran
    # Estate block still present even though the mandate loader failed.
    assert "ESTATE / US-SITUS RULE" in captured["user_constraints"]


# --- orchestrator hands subject_type through ---------------------------------


@pytest.mark.asyncio
async def test_run_funnel_passes_subject_type_to_deep_decision(sf):
    from argosy.services.contracts import FleetPick
    from argosy.services.decision_funnel.orchestrator import run_funnel
    from argosy.services.decision_funnel.triage import TriageOutcome
    from argosy.services.high_potential_funnel import _pick_to_json
    from argosy.state.models import ScanState

    _seed_snapshot(sf, _POSITIONS)
    with sf() as s:
        s.add(
            ScanState(
                user_id="ariel", ticker="RKLB", status="active",
                fleet_json=_pick_to_json(
                    FleetPick(
                        ticker="RKLB", conviction="HIGH", thesis_md="t",
                        verdict="BUY", cites=["10-K"],
                    )
                ),
            )
        )
        s.commit()

    def _triage_go(candidate, **kwargs):
        return TriageOutcome(
            subject=candidate.subject, warrants_decision=True, urgency="HIGH",
            rationale="material", model="claude-sonnet-4-6", prompt_hash="h",
            tokens_in=1, tokens_out=1, cost_usd=0.0,
        )

    seen: dict[str, str] = {}

    async def _deep(*, user_id, ticker, funnel_meta=None, **kwargs):
        seen[ticker] = kwargs.get("subject_type")
        return DeepDecisionOutcome(
            ticker=ticker, status="blocked", blocked_reason="test",
            blocked_by="fund_manager",
        )

    settings = SimpleNamespace(
        decision_funnel_shadow=True, decision_funnel_stage3=True
    )
    await run_funnel(
        "ariel", now=NOW, session_factory=sf, triage_fn=_triage_go,
        deep_decision_fn=_deep, settings=settings,
    )
    assert seen.get("RKLB") == "discovery"
