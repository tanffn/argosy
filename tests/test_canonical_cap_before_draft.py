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
    """The live failure: run 456 mid-phase-3, no PlanVersion yet."""
    values = _values_with_analyst_cap()
    _apply_canonical_allocation(session, 456, values, user_id="ariel")
    assert values["concentration.nvda_cap_pct"].value == pytest.approx(0.13)
    assert "target_allocation_doc" in (
        values["concentration.nvda_cap_pct"].source_locator
    )


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


def test_no_doc_anywhere_leaves_the_analyst_value_untouched(session):
    """Absent is the safe direction — never fabricate a cap."""
    from argosy.state.models import PlanVersion
    session.query(PlanVersion).delete()
    session.commit()
    values = _values_with_analyst_cap()
    _apply_canonical_allocation(session, 456, values, user_id="ariel")
    assert values["concentration.nvda_cap_pct"].value == pytest.approx(0.12)
    assert "concentration.nvda_analyst_floor_pct" not in values
