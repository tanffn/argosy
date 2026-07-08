"""make_thesis_fetcher — plan stance per ticker (2026-07-08 tracking-state audit).

FIX 1: a plan-named instrument's thesis text must include its RECORDED exit
triggers / review anchor so the decision agent judges weakened/broken against
the plan's stated invalidation conditions, not vibes.

FIX 2: a holding the plan does NOT name but whose exposure covers a plan class
(SCHD/FWRA-style substitutes) gets the CLASS rationale, honestly labelled —
not the misleading "not a plan-target instrument". True off-plan singles keep
the honest placeholder.
"""
from __future__ import annotations

import pytest

import argosy.services.stock_decision.fetchers as fetchers_mod
from argosy.services.stock_decision.fetchers import (
    make_thesis_fetcher,
    render_instrument_monitoring_meta,
)
from argosy.services.target_allocation_doc import (
    AllocationClassDoc,
    AllocationInstrument,
    TargetAllocationDoc,
)


def _doc() -> TargetAllocationDoc:
    return TargetAllocationDoc(
        anchor_sigma=0.18, blended_sigma=0.18, nvda_cap_pct=13.0, fi_pct=10.0,
        provenance="test",
        classes=[
            AllocationClassDoc(
                label="Dividend-quality income",
                snapshot_category="Dividend",
                sigma_class="dividend_quality",
                target_pct=12.0,
                rationale="quality dividend growers fund the FI floor",
                instruments=[
                    AllocationInstrument(
                        symbol="FUSA", role="primary",
                        weight_within_class_pct=100.0,
                        rationale="UCITS dividend-quality primary",
                    ),
                ],
            ),
            AllocationClassDoc(
                label="High-growth / high-potential",
                snapshot_category="Individual Stocks",
                sigma_class="high_growth_basket",
                target_pct=5.0,
                rationale="x10 asymmetry sleeve",
                instruments=[
                    AllocationInstrument(
                        symbol="TEM", role="primary",
                        weight_within_class_pct=50.0,
                        rationale="AI diagnostics data moat",
                        exit_triggers=[
                            "oncology read-out fails",
                            "loses data-moat vs Epic",
                        ],
                        review_on="2026-09-30",
                    ),
                ],
            ),
        ],
        glide=[],
    )


class _PV:
    def __init__(self, doc: TargetAllocationDoc) -> None:
        self.target_allocation_json = doc.model_dump_json()


@pytest.fixture
def fetch(monkeypatch):
    monkeypatch.setattr(
        "argosy.state.queries.get_current_plan", lambda db, user_id: _PV(_doc())
    )
    # Exposure-aware attribution map (normally derived from the live snapshot
    # via build_allocation_breakdown) — injected for a hermetic test.
    monkeypatch.setattr(
        fetchers_mod, "_class_labels_by_symbol",
        lambda db, user_id, doc: {"SCHD": "Dividend-quality income"},
    )
    return make_thesis_fetcher(db=object(), user_id="ariel")


def test_plan_named_instrument_includes_exit_triggers(fetch):
    out = fetch("TEM")
    assert "High-growth / high-potential" in out
    assert "AI diagnostics data moat" in out
    assert "EXIT TRIGGERS" in out
    assert "oncology read-out fails" in out
    assert "loses data-moat vs Epic" in out
    assert "Review on: 2026-09-30" in out


def test_plan_named_instrument_without_triggers_has_no_meta_block(fetch):
    out = fetch("FUSA")
    assert "Dividend-quality income" in out
    assert "EXIT TRIGGERS" not in out
    assert "Review on:" not in out


def test_substitute_holding_gets_class_thesis_honestly_labelled(fetch):
    """SCHD (not plan-named) covers the dividend sleeve — it must get the CLASS
    rationale labelled as substitute coverage, not the off-plan placeholder."""
    out = fetch("SCHD")
    assert "covers the 'Dividend-quality income' sleeve" in out
    assert "substitute for FUSA" in out
    assert "quality dividend growers fund the FI floor" in out
    assert "not plan-named" in out
    assert "not a plan-target instrument" not in out


def test_true_offplan_single_keeps_honest_placeholder(fetch):
    assert fetch("RKT") == (
        "not a plan-target instrument (candidate or legacy holding)"
    )


def test_render_instrument_monitoring_meta_empty_for_bare_instrument():
    inst = AllocationInstrument(
        symbol="CSPX", role="primary", weight_within_class_pct=100.0
    )
    assert render_instrument_monitoring_meta(inst) == ""


def test_fetcher_degrades_when_no_plan(monkeypatch):
    monkeypatch.setattr(
        "argosy.state.queries.get_current_plan", lambda db, user_id: None
    )
    fetch = make_thesis_fetcher(db=object(), user_id="ariel")
    assert fetch("TEM") is None
