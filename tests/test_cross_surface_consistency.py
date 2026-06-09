"""THE cross-surface reconciliation guardrail (roadmap T2.5).

The 12+-session ask, made executable: when a plan version carries the canonical
``TargetAllocationDoc``, every surface must PROJECT that one object — never
re-derive its own divergent view. This test fails loudly on drift.

Guardrail-first: written RED before the surface rebinds (T2.1-T2.4); each rebind
turns one assertion group GREEN. It is the acceptance bar for P2 — a surface
number that can't reconcile to the doc is a defect.
"""
from __future__ import annotations

from datetime import date

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.allocation_glidepath import compute_allocation_glidepath
from argosy.services.allocation_plan import build_target_allocation
from argosy.services.target_allocation_doc import (
    build_target_allocation_doc,
    derive_full_book_today_composition,
    load_plan_target_allocation,
)
from argosy.state.models import Base, PlanVersion, User
from argosy.state.queries import get_current_plan

_TODAY = date(2026, 6, 9)
NVDA_LABEL = "Strategic single-stock (NVDA)"

# The live snapshot ex-NVDA categories (normalized) — the basis codex verified.
_EX_NVDA = {
    "alternative": 0.0,
    "cash": 12.94,
    "core equity": 26.25,
    "defensive": 11.06,
    "dividend": 18.28,
    "growth": 10.9,
    "individual stocks": 18.21,
    "international": 2.36,
}


def _nvda_band(comp: dict[str, float]) -> float:
    """NVDA's weight in a composition dict, robust to label vs lowercased keys.
    Matches the strategic single-stock sleeve only — NOT 'US growth tilt
    (ex-NVDA)' nor 'Individual Stocks (non-NVDA, to redeploy)' (both contain the
    'nvda' substring) nor the 'individual stocks' other-singles row."""
    return sum(
        v for k, v in comp.items()
        if "strategic single-stock" in k.lower() or k.strip().upper() == "NVDA"
    )


def _canonical_doc():
    by_label = {c.label: c.target_pct for c in build_target_allocation().classes}
    comp = derive_full_book_today_composition(
        nvda_tradeable_pct=64.86,
        ex_nvda_categories=_EX_NVDA,
        low_vol_target=by_label["US low-volatility equity"],
        bonds_target=by_label["Short-duration IG bonds"],
    )
    return build_target_allocation_doc(today=_TODAY, today_composition=comp)


def _seed_plan_with_doc(tmp_path):
    """A current plan carrying the canonical doc (no snapshot / horizon JSON —
    the doc is the single source the surfaces must read)."""
    eng = sa.create_engine(
        f"sqlite:///{tmp_path / 'guardrail.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng, expire_on_commit=False)()
    db.add(User(id="ariel", plan="free"))
    db.flush()
    db.add(
        PlanVersion(
            user_id="ariel",
            role="current",
            version_label="guardrail",
            source_path="",
            raw_markdown="",
            target_allocation_json=_canonical_doc().model_dump_json(),
        )
    )
    db.commit()
    return db


def test_doc_is_the_seeded_single_source(tmp_path) -> None:
    db = _seed_plan_with_doc(tmp_path)
    doc = load_plan_target_allocation(get_current_plan(db, "ariel"))
    assert doc is not None
    assert doc.glide[0].composition_pct_by_class[NVDA_LABEL] == pytest.approx(64.86, abs=0.01)
    assert doc.glide[-1].composition_pct_by_class[NVDA_LABEL] == pytest.approx(12.0, abs=0.01)


def test_plan_glidepath_reconciles_to_the_canonical_doc(tmp_path) -> None:
    """T2.1 target: /plan glidepath renders the doc's glide (full-book, NVDA
    64.86 -> 12), NOT the snapshot's other-singles 18.21 nor LLM SynthTargets."""
    db = _seed_plan_with_doc(tmp_path)
    gp = compute_allocation_glidepath(db, "ariel", _TODAY)

    assert gp is not None and gp.points, "glidepath must render from the canonical doc"
    for p in gp.points:
        assert sum(p.composition_pct_by_class.values()) == pytest.approx(100.0, abs=1.0)

    nvda_t0 = _nvda_band(gp.points[0].composition_pct_by_class)
    nvda_end = _nvda_band(gp.points[-1].composition_pct_by_class)
    assert nvda_t0 == pytest.approx(64.86, abs=1.5), (
        f"t0 NVDA band must be the full-book 64.86 (got {nvda_t0}); the "
        "other-singles 18.21 is the root-confusion bug this guardrail kills"
    )
    assert nvda_end == pytest.approx(12.0, abs=1.5), (
        f"glidepath must deconcentrate NVDA to the 12 target (got {nvda_end})"
    )


def test_portfolio_pie_reconciles_to_the_canonical_doc() -> None:
    """T2.2 target: /portfolio's pie IS the plan — current % from the glide's
    today anchor, target % from its endpoint — so it agrees with the /plan
    glidepath label-for-label (one object, two surfaces)."""
    from argosy.api.routes.portfolio import _allocations_from_doc

    doc = _canonical_doc()
    allocs = _allocations_from_doc(doc)
    by_cat = {a.category: a for a in allocs}

    nvda = by_cat[NVDA_LABEL]
    assert nvda.pct == pytest.approx(64.86, abs=0.01)        # current == glide q0
    assert nvda.target_pct == pytest.approx(12.0, abs=0.01)  # target == glide q8

    q0 = doc.glide[0].composition_pct_by_class
    qN = doc.glide[-1].composition_pct_by_class
    for a in allocs:
        assert a.pct == pytest.approx(round(q0.get(a.category, 0.0), 2), abs=0.01)
        assert a.target_pct == pytest.approx(round(qN.get(a.category, 0.0), 2), abs=0.01)


def test_retirement_glide_reconciles_to_the_canonical_doc() -> None:
    """T2.3/T2.5 — the /retirement equity/bond/cash glide projects the doc's
    target allocation (the plan's equity-heavy mix), not a textbook age curve:
    bonds/cash are the doc's FI split, equity is everything else, sum == 100."""
    from argosy.services.target_allocation_doc import doc_equity_bond_cash

    doc = _canonical_doc()
    eq, bd, cs = doc_equity_bond_cash(doc)
    exp_bonds = sum(c.target_pct for c in doc.classes if c.sigma_class == "bonds")
    exp_cash = sum(c.target_pct for c in doc.classes if c.sigma_class == "cash")
    assert eq + bd + cs == pytest.approx(100.0, abs=0.1)
    assert bd == pytest.approx(exp_bonds, abs=0.01)
    assert cs == pytest.approx(exp_cash, abs=0.01)
    assert eq == pytest.approx(100.0 - exp_bonds - exp_cash, abs=0.1)
    assert eq > bd and eq > cs  # the plan is equity-heavy by design
