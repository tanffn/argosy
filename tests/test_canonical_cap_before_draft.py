"""The settled NVDA cap must resolve BEFORE the run writes its draft.

`_apply_canonical_allocation` read the user-settled cap from the PlanVersion
produced by the current decision run. During phase 3 — while the prose is being
authored — that row does not exist yet, so the override silently no-opped and
the synthesizer saw the concentration analyst's derived cap (12%) instead of
the settled binding cap (13%). Once the draft persisted, the SAME call returned
13%.

Result: the prose said 12% while the canonical block said 13%, and codex
blocked the divergence on runs 448, 449 and 456 — a self-inflicted
`C2_NO_CONTRADICTION` that no amount of guidance could fix, because the two
numbers came from two different reads of the same function at different times.

The binding cap is user-settled and independent of this run's output, so it is
inherited from the most recent plan carrying a doc. Per-class target weights
ARE this run's authored output and must NOT be inherited.
"""
from __future__ import annotations

import json

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.plan_numeric_resolver import _apply_canonical_allocation
from argosy.services.retirement.scenario_mc import DEFAULT_NVDA_CAP_PCT


@pytest.fixture
def session(tmp_path):
    from argosy.state.models import Base, DecisionRun, PlanVersion

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'cap.db'}")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()

    doc = json.dumps({
        "nvda_cap_pct": 13.0,
        "classes": [{"label": "Global equity", "target_pct": 60.0}],
    })
    # A settled prior plan, produced by an EARLIER run.
    s.add(DecisionRun(id=100, user_id="ariel", ticker="PLAN",
                      status="completed"))
    s.add(PlanVersion(
        id=1, user_id="ariel", role="current",
        decision_run_id=100, target_allocation_json=doc,
    ))
    # The run currently authoring — no PlanVersion of its own yet.
    s.add(DecisionRun(id=456, user_id="ariel", ticker="PLAN",
                      status="running"))
    s.commit()
    return s


def _values_with_analyst_cap():
    from argosy.services.plan_numeric_resolver import ResolvedValue
    return {
        "concentration.nvda_cap_pct": ResolvedValue(
            key="concentration.nvda_cap_pct", value=0.12, unit="pct",
            status="resolved", source_locator="concentration.nvda_cap_pct",
            confidence="LOW",
            formula="MIN over four constraint caps (sequence/tail/risk/tax)",
        )
    }


def test_settled_cap_governs_while_the_draft_is_being_authored(session):
    """The live failure: run 456 mid-phase-3, no PlanVersion yet.

    The cap comes from the CONSTANT, not from a prior plan's persisted
    snapshot — the constant is what this run will write into its own doc
    minutes later, so authoring and persistence agree even across a tuning
    of the constant. Asserted against the constant rather than a literal so
    tuning it does not break this pin.
    """
    values = _values_with_analyst_cap()
    _apply_canonical_allocation(session, 456, values, user_id="ariel")
    assert values["concentration.nvda_cap_pct"].value == pytest.approx(
        DEFAULT_NVDA_CAP_PCT
    )
    assert "DEFAULT_NVDA_CAP_PCT" in (
        values["concentration.nvda_cap_pct"].source_locator
    )


def test_authoring_cap_matches_what_the_run_will_persist(session):
    """The regression that motivated reading the constant directly.

    A prior plan carrying a STALE cap (13%) must not drive the prose when
    the constant has since moved, or the draft contradicts its own doc.
    """
    from argosy.state.models import PlanVersion
    import sqlalchemy as _sa
    stale = session.execute(
        _sa.select(PlanVersion).where(PlanVersion.id == 1)
    ).scalar_one()
    assert '"nvda_cap_pct": 13.0' in stale.target_allocation_json

    values = _values_with_analyst_cap()
    _apply_canonical_allocation(session, 456, values, user_id="ariel")
    assert values["concentration.nvda_cap_pct"].value == pytest.approx(
        DEFAULT_NVDA_CAP_PCT
    ), "authored from the stale prior snapshot instead of the constant"


def test_analyst_cap_is_preserved_as_a_subordinate_floor(session):
    values = _values_with_analyst_cap()
    _apply_canonical_allocation(session, 456, values, user_id="ariel")
    floor = values["concentration.nvda_analyst_floor_pct"]
    assert floor.value == pytest.approx(0.12)
    # The value is preserved; the formula is relabelled to make its
    # SUBORDINATE status explicit, so the plan can cite both and neither
    # reads as the governing ceiling.
    assert "subordinate" in floor.formula.lower()
    assert floor.source_locator == "concentration.nvda_cap_pct"


def test_class_weights_are_not_inherited_from_the_prior_plan(session):
    """Strategic weights are THIS run's authored output — absent is correct;
    the gate then flags them, which is the safe direction."""
    values = _values_with_analyst_cap()
    _apply_canonical_allocation(session, 456, values, user_id="ariel")
    assert not [
        k for k in values
        if k.startswith("allocation.") and k.endswith("_target_pct")
    ]


def test_own_draft_still_wins_and_carries_its_class_weights(session):
    """When the run HAS written its draft, nothing is inherited."""
    from argosy.state.models import PlanVersion
    session.add(PlanVersion(
        id=2, user_id="ariel", role="draft", decision_run_id=456,
        target_allocation_json=json.dumps({
            "nvda_cap_pct": 13.0,
            "classes": [{"label": "Global equity", "target_pct": 55.0}],
        }),
    ))
    session.commit()
    values = _values_with_analyst_cap()
    _apply_canonical_allocation(session, 456, values, user_id="ariel")
    assert values["concentration.nvda_cap_pct"].value == pytest.approx(0.13)
    assert values["allocation.global_equity_target_pct"].value == pytest.approx(0.55)


def test_no_plan_history_still_yields_the_governing_ceiling(session):
    """With no plan history at all the constant still governs — the ceiling
    is a policy parameter, not something derived from past drafts."""
    from argosy.state.models import PlanVersion
    session.query(PlanVersion).delete()
    session.commit()
    values = _values_with_analyst_cap()
    _apply_canonical_allocation(session, 456, values, user_id="ariel")
    assert values["concentration.nvda_cap_pct"].value == pytest.approx(
        DEFAULT_NVDA_CAP_PCT
    )
    assert values["concentration.nvda_analyst_floor_pct"].value == pytest.approx(
        0.12
    )
