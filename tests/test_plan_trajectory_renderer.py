"""Renderer tests for the resolver-backed trajectory appendix.

Asserts the appendix contains ZERO hardcoded headline constants and that
every figure traces to the plan-numeric resolver:

  * fully-seeded run → appendix shows the DERIVED figures, and the old
    hardcoded literals ("0.821", "341000", "21.0", "age 49", "age 44",
    "97-98%") are ABSENT;
  * a pending source → "[derivation pending]" appears for that figure and
    the dependent trajectory rows are annotated, not fabricated.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.orchestrator.flows.plan_synthesis.render import (
    render_trajectory_reconciliation_appendix,
)
from argosy.state.models import AgentReport, Base, PortfolioSnapshotRow, User

# Reuse the typed-payload builders from the resolver test.
from tests.test_plan_numeric_resolver import (
    _concentration_json,
    _equity_comp_json,
    _household_budget_json,
    _withdrawal_sequencer_json,
)

DRUN = 71
DECISION_ID = f"plan-synth-{DRUN}"

# The hardcoded constants the old renderer baked in — none may survive.
FORBIDDEN_LITERALS = ["0.821", "341,000", "341000", "21.0", "age 49", "age 44", "97-98%"]


@pytest.fixture
def session(tmp_path):
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'traj.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SF = sessionmaker(bind=engine, expire_on_commit=False)
    s = SF()
    try:
        s.add(User(id="ariel", plan="free"))
        s.flush()
        yield s
    finally:
        s.close()
        engine.dispose()


def _seed_snapshot(s, *, total_usd_k=3_096.0, fx=3.45):
    s.add(
        PortfolioSnapshotRow(
            user_id="ariel",
            imported_at=datetime(2026, 6, 1),
            totals_json=json.dumps({"total_usd_value_k": total_usd_k}),
            fx_usd_nis=fx,
        )
    )
    s.flush()


def _seed_report(s, role, text):
    s.add(
        AgentReport(
            user_id="ariel",
            agent_role=role,
            decision_id=DECISION_ID,
            prompt_hash="h",
            response_text=text,
        )
    )
    s.flush()


def _seed_all(s):
    _seed_snapshot(s)
    _seed_report(s, "withdrawal_sequencer", _withdrawal_sequencer_json())
    _seed_report(s, "equity_comp_analyst", _equity_comp_json())
    _seed_report(s, "household_budget", _household_budget_json())
    _seed_report(s, "concentration", _concentration_json())
    s.commit()


# ---------------------------------------------------------------------------
# Fully-seeded → derived figures, no hardcoded constants
# ---------------------------------------------------------------------------


def test_appendix_contains_no_hardcoded_constants(session):
    _seed_all(session)
    md = render_trajectory_reconciliation_appendix(
        session=session, user_id="ariel", decision_run_id=DRUN
    )
    assert md, "appendix should render with a full seed"
    for lit in FORBIDDEN_LITERALS:
        assert lit not in md, f"hardcoded literal {lit!r} leaked into the appendix"


def test_appendix_shows_derived_figures(session):
    _seed_all(session)
    md = render_trajectory_reconciliation_appendix(
        session=session, user_id="ariel", decision_run_id=DRUN
    )
    # Derived net worth = 3096 * 1000 * 3.45 = 10,681,200 → ₪10.68M.
    assert "₪10.68M" in md
    # FI target now comes from the deterministic fi_methodology, not the
    # agent's fi_base: permanent-equivalent spend (T12 276,996 + life-event
    # params 70,000 = 346,996) ÷ 3.0% SWR = ₪11.57M. FI age 52 (51.7) still
    # from the withdrawal_sequencer.
    assert "₪11.57M" in md
    assert "age 52" in md
    # Resolver source keys are surfaced for traceability.
    assert "retirement.fi_target_nis" in md
    assert "savings.annual_net_nis" in md
    # The "where you are today" surface leads with the LIQUID FI basis (the
    # same basis the front-door fi_margin uses), so the trajectory cannot read
    # as "FI reached" off the investable figure.
    assert "portfolio.liquid_net_worth_nis" in md
    # The trajectory grows at the 5.0% real return; the FI target is sized on
    # the decoupled 3.0% perpetual SWR — both surfaced from the resolver.
    assert "5.0% real" in md
    assert "3.00%" in md
    # No pending FIGURE when everything is seeded. The intro paragraph
    # mentions the convention `[derivation pending]` once as documentation;
    # strip that single explanatory mention before asserting no figure is
    # pending.
    body = md.replace("shows as `[derivation pending]`", "")
    assert "[derivation pending]" not in body


# ---------------------------------------------------------------------------
# Pending source → [derivation pending], no fabrication
# ---------------------------------------------------------------------------


def test_appendix_labels_three_age_definitions_distinctly(session):
    """Coherence guard (run-102 reader BLOCKER): the derived-FI age, the
    Monte-Carlo earliest-safe age, and the per-scenario target age must each
    render with a DISTINCT, self-describing qualifier so a client never reads
    the three different numbers as a self-contradiction.
    """
    _seed_all(session)
    md = render_trajectory_reconciliation_appendix(
        session=session, user_id="ariel", decision_run_id=DRUN
    )
    # 1) Derived FI age must carry the deterministic / perpetuity qualifier.
    assert "Derived FI age (deterministic, perpetuity basis)" in md
    # 2) The reconciliation prose must qualify the Monte-Carlo earliest-safe
    #    headline age distinctly, so it never reads as the same concept as the
    #    deterministic FI age.
    assert "earliest safe retirement age* (Monte-Carlo, 90% solvency" in md
    # 3) The reconciliation prose must frame these as three DIFFERENT valid
    #    definitions, not a contradiction, and name the per-scenario age.
    assert "three DIFFERENT" in md
    assert "per-scenario" in md
    # The bare, unqualified "Derived FI age |" label must be gone.
    assert "| Derived FI age | " not in md


def test_appendix_cross_references_mc_earliest_safe_age_when_resolved(session):
    """When the Monte-Carlo earliest-safe age IS resolved it gets its own,
    distinctly-qualified row in the reconciliation table (cross-referencing
    the deterministic FI age). When unresolved the row is omitted rather than
    rendering a fabricated `[derivation pending]` age."""
    _seed_all(session)
    md = render_trajectory_reconciliation_appendix(
        session=session, user_id="ariel", decision_run_id=DRUN
    )
    # The in-memory fixture cannot resolve the MC earliest-safe age (needs the
    # retirement engine) → the cross-reference ROW must be ABSENT, never a
    # fabricated pending age.
    assert (
        "| Earliest safe retirement age (Monte-Carlo, 90% solvency to 95, "
        "typical-drawdown) | [derivation pending] |"
    ) not in md


def test_pending_fi_target_renders_derivation_pending(session):
    # FI target/spend/yield/return now come from the deterministic
    # fi_methodology, fed by the tracked baseline spend. To make the FI
    # target PENDING we must deny it any baseline: NO household_budget AND no
    # UserContext (this in-memory DB has none). withdrawal_sequencer is also
    # absent so fi_age is pending too.
    _seed_snapshot(session)
    _seed_report(session, "equity_comp_analyst", _equity_comp_json())
    _seed_report(session, "concentration", _concentration_json())
    session.commit()

    md = render_trajectory_reconciliation_appendix(
        session=session, user_id="ariel", decision_run_id=DRUN
    )
    assert md, "appendix still renders (net worth resolved)"
    assert "[derivation pending]" in md
    # The FI target line is pending, NOT a fabricated number.
    assert "retirement.fi_target_nis" in md
    # Trajectory rows depend on the (now-pending) return assumption →
    # they must render the pending label, not a projected balance.
    assert "Trajectory rows are `[derivation pending]`" in md


def test_pending_savings_blocks_trajectory_rows(session):
    # Seed everything EXCEPT equity_comp_analyst (owns savings = annuity C).
    _seed_snapshot(session)
    _seed_report(session, "withdrawal_sequencer", _withdrawal_sequencer_json())
    _seed_report(session, "household_budget", _household_budget_json())
    _seed_report(session, "concentration", _concentration_json())
    session.commit()

    md = render_trajectory_reconciliation_appendix(
        session=session, user_id="ariel", decision_run_id=DRUN
    )
    # FI target now from fi_methodology (household_budget seeded) = ₪11.57M;
    # FI age 52 still from the withdrawal_sequencer.
    assert "₪11.57M" in md
    assert "age 52" in md
    # But the savings line + forward trajectory are pending.
    assert "[derivation pending]" in md
    assert "Trajectory rows are `[derivation pending]`" in md


def test_no_snapshot_returns_empty(session):
    # No snapshot at all → no starting point → empty appendix (cannot
    # draw any trajectory; never fabricate P0).
    _seed_report(session, "withdrawal_sequencer", _withdrawal_sequencer_json())
    session.commit()
    md = render_trajectory_reconciliation_appendix(
        session=session, user_id="ariel", decision_run_id=DRUN
    )
    assert md == ""


def test_no_decision_run_id_returns_empty(session):
    _seed_all(session)
    md = render_trajectory_reconciliation_appendix(
        session=session, user_id="ariel", decision_run_id=None
    )
    assert md == ""
