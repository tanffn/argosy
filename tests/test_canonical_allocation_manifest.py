"""`_apply_canonical_allocation` — register the canonical TargetAllocationDoc
weights + structural ages as RESOLVED values so the headline-numeric-source
gate can trace every Argosy-derived allocation number the plan prose cites.
"""
from __future__ import annotations

import json

from argosy.services.plan_numeric_resolver import _apply_canonical_allocation
from argosy.state.models import PlanVersion, User


_DOC = {
    "nvda_cap_pct": 13.0,
    "classes": [
        {"label": "US broad-market core", "target_pct": 25.94},
        {"label": "Strategic single-stock (NVDA)", "target_pct": 12.0},
        {"label": "Cash & T-bills (incl. ILS tranche)", "target_pct": 14.91},
    ],
    "glide": [],
}


def _seed_plan(session_factory) -> int:
    sess = session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
            sess.commit()
        pv = PlanVersion(
            user_id="ariel",
            role="draft",
            version_label="alloc-manifest-test",
            raw_markdown="",
            decision_run_id=4242,
            target_allocation_json=json.dumps(_DOC),
        )
        sess.add(pv)
        sess.commit()
        return pv.id
    finally:
        sess.close()


def test_registers_class_targets_nvda_and_structural_ages(client_with_db):
    _seed_plan(client_with_db.app.state.session_factory)
    sess = client_with_db.app.state.session_factory()
    try:
        values: dict = {}
        _apply_canonical_allocation(sess, 4242, values)
    finally:
        sess.close()

    # Each class target is registered as a FRACTION (gate scales ×100).
    pct_vals = {round(v.value, 4) for v in values.values() if v.unit == "pct"}
    assert 0.12 in pct_vals  # NVDA strategic target 12%
    assert 0.2594 in pct_vals  # core 25.94%
    assert 0.1491 in pct_vals  # cash 14.91%
    assert 0.13 in pct_vals  # concentration cap 13%

    # Structural ages the prose cites are resolved (not fabrications).
    age_vals = {v.value for v in values.values() if v.unit == "age"}
    assert {60.0, 67.0, 95.0} <= age_vals

    # Every registered value is RESOLVED with a real source locator (auditable).
    for v in values.values():
        assert v.status == "resolved"
        assert v.source_locator


def test_absent_doc_registers_nothing_for_allocation(client_with_db):
    """No matching plan version → no allocation keys (gate then flags the
    numbers — the safe direction, never a fabricated trace)."""
    sess = client_with_db.app.state.session_factory()
    try:
        values: dict = {}
        _apply_canonical_allocation(sess, 999999, values)
    finally:
        sess.close()
    assert not [k for k in values if k.startswith("allocation.")]
