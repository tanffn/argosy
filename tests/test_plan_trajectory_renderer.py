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
    # Derived FI target ₪8.0M and FI age 52 (51.7 rounded).
    assert "₪8.00M" in md
    assert "age 52" in md
    # Resolver source keys are surfaced for traceability.
    assert "retirement.fi_target_nis" in md
    assert "savings.annual_net_nis" in md
    assert "portfolio.net_worth_nis" in md
    # Real-return assumption rendered from the resolver (4.5%).
    assert "4.5% real" in md
    # No pending FIGURE when everything is seeded. The intro paragraph
    # mentions the convention `[derivation pending]` once as documentation;
    # strip that single explanatory mention before asserting no figure is
    # pending.
    body = md.replace("shows as `[derivation pending]`", "")
    assert "[derivation pending]" not in body


# ---------------------------------------------------------------------------
# Pending source → [derivation pending], no fabrication
# ---------------------------------------------------------------------------


def test_pending_fi_target_renders_derivation_pending(session):
    # Seed everything EXCEPT withdrawal_sequencer (owns fi_target + return).
    _seed_snapshot(session)
    _seed_report(session, "equity_comp_analyst", _equity_comp_json())
    _seed_report(session, "household_budget", _household_budget_json())
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
    # FI target + age still derived (withdrawal_sequencer present).
    assert "₪8.00M" in md
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
