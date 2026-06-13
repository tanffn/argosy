"""Tests for the canonical instrument-level TargetAllocationDoc (roadmap T1.x).

The doc is the single structured object every surface reads: instrument-level
(tickers per class), canonical (engine-authored), and time-varying (a quarterly
glide). T1.1 covers the schema + JSON round-trip; later tasks add the builder.
"""

from __future__ import annotations

from datetime import date

import pytest

from argosy.services.target_allocation_doc import (
    OTHER_SINGLES_LABEL,
    AllocationClassDoc,
    AllocationInstrument,
    GlideWaypoint,
    TargetAllocationDoc,
    _deconcentration_quarters,
    build_target_allocation_doc,
    derive_full_book_today_composition,
    doc_equity_bond_cash,
    load_plan_target_allocation,
)

# A full liquid book incl. NVDA (sums to ~100) — the settled basis is the FULL
# tradeable book, NVDA ~60%. Passed explicitly so the builder stays pure; the
# snapshot-derivation of this composition is a wiring concern (T1.5/T1.6),
# verified separately because the basis is money-math-sensitive.
_TODAY_FULL_BOOK = {
    "Strategic single-stock (NVDA)": 60.47,
    "US growth tilt (ex-NVDA)": 11.04,
    "US broad-market core": 10.53,
    "Dividend-quality income": 7.01,
    "Cash & T-bills (incl. ILS tranche)": 4.95,
    "Short-duration IG bonds": 3.29,
    "Real assets (REIT/TIPS)": 1.82,
    "International developed (ex-US)": 0.90,
}


def test_doc_is_instrument_level_and_roundtrips() -> None:
    doc = TargetAllocationDoc(
        anchor_sigma=0.18,
        blended_sigma=0.18,
        nvda_cap_pct=13.0,
        fi_pct=21.3,
        provenance="panel",
        classes=[
            AllocationClassDoc(
                label="Strategic single-stock (NVDA)",
                snapshot_category="Individual Stocks",
                sigma_class="concentrated_equity",
                target_pct=12.0,
                instruments=[
                    AllocationInstrument(
                        symbol="NVDA", role="primary", weight_within_class_pct=100.0
                    )
                ],
            ),
        ],
        glide=[
            GlideWaypoint(
                quarter=1,
                date=date(2026, 9, 1),
                composition_pct_by_class={"Strategic single-stock (NVDA)": 12.0},
            )
        ],
    )

    again = TargetAllocationDoc.model_validate_json(doc.model_dump_json())

    assert again.classes[0].instruments[0].symbol == "NVDA"
    assert again.glide[0].composition_pct_by_class["Strategic single-stock (NVDA)"] == 12.0


def test_doc_carries_schema_defaults() -> None:
    """schema_version + basis default without being supplied (provenance contract)."""
    doc = TargetAllocationDoc(
        anchor_sigma=0.18,
        blended_sigma=0.18,
        nvda_cap_pct=13.0,
        fi_pct=21.3,
        provenance="panel",
        classes=[],
        glide=[],
    )
    assert doc.schema_version == 1
    assert doc.basis == "full tradeable book"


def test_builds_instrument_level_doc_with_quarterly_glide() -> None:
    doc = build_target_allocation_doc(
        today=date(2026, 6, 9),
        today_composition=_TODAY_FULL_BOOK,
        quarters=8,
    )

    # instrument-level: every class names its tickers
    assert all(c.instruments for c in doc.classes)
    nvda = next(c for c in doc.classes if c.snapshot_category == "Individual Stocks")
    assert [i.symbol for i in nvda.instruments] == ["NVDA"]

    # the 13% hard cap is carried, not re-derived
    assert doc.nvda_cap_pct == pytest.approx(13.0)

    # the glide is q0 (today anchor) + 8 quarterly waypoints, each a coherent 100%
    assert len(doc.glide) == 9
    assert doc.glide[0].quarter == 0
    for wp in doc.glide:
        assert sum(wp.composition_pct_by_class.values()) == pytest.approx(100.0, abs=0.1)

    nvda_label = "Strategic single-stock (NVDA)"
    # q0 reflects TODAY exactly (the chart's left anchor == /portfolio's reality)
    assert doc.glide[0].composition_pct_by_class[nvda_label] == pytest.approx(
        _TODAY_FULL_BOOK[nvda_label], abs=0.01
    )

    # the final waypoint lands exactly on the end-state class targets
    final = doc.glide[-1].composition_pct_by_class
    for c in doc.classes:
        assert final[c.label] == pytest.approx(c.target_pct, abs=0.1)

    # NVDA deconcentrates: bigger today than at the target
    assert (
        doc.glide[0].composition_pct_by_class[nvda_label]
        > doc.glide[-1].composition_pct_by_class[nvda_label]
    )


class _PV:
    """Minimal PlanVersion stand-in carrying just the column the reader reads."""

    def __init__(self, target_allocation_json: str | None) -> None:
        self.target_allocation_json = target_allocation_json


def test_doc_equity_bond_cash_aggregates_by_sigma_class() -> None:
    """T2.3 — the retirement /glide-path projects the doc's equity/bond/cash
    (the plan's actual, equity-heavy target), summing to ~100."""
    doc = build_target_allocation_doc(
        today=date(2026, 6, 9), today_composition=_TODAY_FULL_BOOK
    )
    eq, bd, cs = doc_equity_bond_cash(doc)
    assert eq + bd + cs == pytest.approx(100.0, abs=0.1)
    # equity dominates (NVDA + the equity sleeves); bonds + cash are the FI split
    assert eq > bd and eq > cs
    assert bd > 0 and cs > 0
    # NVDA (concentrated_equity, 12%) folds into equity, not its own bucket
    assert eq >= 12.0


def test_load_plan_target_allocation_parses_when_set() -> None:
    doc = build_target_allocation_doc(
        today=date(2026, 6, 9), today_composition=_TODAY_FULL_BOOK
    )
    loaded = load_plan_target_allocation(_PV(doc.model_dump_json()))
    assert loaded is not None
    assert loaded.classes[0].instruments  # instrument-level survives the round-trip


def test_load_plan_target_allocation_returns_none_and_never_raises() -> None:
    # empty / missing column -> None
    assert load_plan_target_allocation(_PV(None)) is None
    assert load_plan_target_allocation(_PV("")) is None
    # malformed JSON -> None (never raises)
    assert load_plan_target_allocation(_PV("{not valid json")) is None
    # object with no such attribute at all -> None
    assert load_plan_target_allocation(object()) is None


# The live snapshot (portfolio_snapshots.id=1) ex-NVDA categories, normalized
# keys as _categories_from_snapshot returns them. NVDA is NOT here — it is a
# separate positions row; its weight comes from the concentration report.
_SNAPSHOT_EX_NVDA = {
    "alternative": 0.0,
    "cash": 12.94,
    "core equity": 26.25,
    "defensive": 11.06,
    "dividend": 18.28,
    "growth": 10.9,
    "individual stocks": 18.21,
    "international": 2.36,
}


def test_derive_full_book_composition_matches_codex_verified() -> None:
    """Reconciles to the codex danger-full-access verdict against the live DB:
    NVDA 64.86% (concentration report, NOT the snapshot 'Individual Stocks'),
    ex-NVDA categories scaled by (100-64.86)/100, Defensive split low-vol/bonds
    by target ratio, other-singles modeled as a distinct redeploy band."""
    comp = derive_full_book_today_composition(
        nvda_tradeable_pct=64.86,
        ex_nvda_categories=_SNAPSHOT_EX_NVDA,
        low_vol_target=5.56,
        bonds_target=6.39,
    )
    assert sum(comp.values()) == pytest.approx(100.0, abs=0.01)
    assert comp["Strategic single-stock (NVDA)"] == pytest.approx(64.86)
    assert comp["US broad-market core"] == pytest.approx(9.2242, abs=0.001)
    assert comp["Dividend-quality income"] == pytest.approx(6.4236, abs=0.001)
    assert comp["International developed (ex-US)"] == pytest.approx(0.8293, abs=0.001)
    # growth ALONE (10.9 x 0.3514), NOT folded with the other singles
    assert comp["US growth tilt (ex-NVDA)"] == pytest.approx(3.8303, abs=0.001)
    assert comp["US low-volatility equity"] == pytest.approx(1.8083, abs=0.001)
    assert comp["Short-duration IG bonds"] == pytest.approx(2.0782, abs=0.001)
    assert comp["Cash & T-bills (incl. ILS tranche)"] == pytest.approx(4.5471, abs=0.001)
    assert comp["Real assets (REIT/TIPS)"] == pytest.approx(0.0, abs=0.001)
    # the non-NVDA singles are an honest, distinct redeploy band (-> glides to 0)
    assert comp[OTHER_SINGLES_LABEL] == pytest.approx(6.399, abs=0.001)


# ─── T4.2: deconcentration glide horizon follows the optimizer ─────────────


def test_deconcentration_quarters_follows_optimizer_chosen_horizon(monkeypatch) -> None:
    from argosy.services.retirement import deconcentration_optimizer as deco

    class _Plan:
        chosen_horizon_years = 3

    monkeypatch.setattr(deco, "optimize_deconcentration", lambda **_k: _Plan())
    # H=3y → 12 quarters (distinct from the fixed-2yr default of 8).
    assert _deconcentration_quarters(db=None, user_id="ariel", today=date(2026, 6, 9)) == 12


def test_deconcentration_quarters_falls_back_when_no_feasible_horizon(monkeypatch) -> None:
    from argosy.services.retirement import deconcentration_optimizer as deco

    class _Plan:
        chosen_horizon_years = None

    monkeypatch.setattr(deco, "optimize_deconcentration", lambda **_k: _Plan())
    assert _deconcentration_quarters(db=None, user_id="ariel", today=date(2026, 6, 9)) == 8


def test_deconcentration_quarters_falls_back_on_optimizer_error(monkeypatch) -> None:
    from argosy.services.retirement import deconcentration_optimizer as deco

    def _boom(**_k):
        raise RuntimeError("heavy MC failed")

    monkeypatch.setattr(deco, "optimize_deconcentration", _boom)
    assert _deconcentration_quarters(db=None, user_id="ariel", today=date(2026, 6, 9)) == 8


def test_derived_composition_drives_a_two_sided_glide() -> None:
    """The derived composition + the engine target produce a glide where BOTH
    NVDA and the legacy singles deconcentrate toward the target (redeploy)."""
    comp = derive_full_book_today_composition(
        nvda_tradeable_pct=64.86,
        ex_nvda_categories=_SNAPSHOT_EX_NVDA,
        low_vol_target=5.56,
        bonds_target=6.39,
    )
    doc = build_target_allocation_doc(
        today=date(2026, 6, 9), today_composition=comp, quarters=8
    )
    nvda = "Strategic single-stock (NVDA)"
    # q0 anchors on today's real NVDA weight (64.86%); it then glides toward the
    # 12 target; the legacy singles band glides to ~0; every quarter sums to 100.
    assert doc.glide[0].composition_pct_by_class[nvda] == pytest.approx(64.86, abs=0.01)
    assert doc.glide[0].composition_pct_by_class[nvda] > doc.glide[-1].composition_pct_by_class[nvda]
    assert doc.glide[-1].composition_pct_by_class.get(OTHER_SINGLES_LABEL, 0.0) == pytest.approx(0.0, abs=0.01)
    for wp in doc.glide:
        assert sum(wp.composition_pct_by_class.values()) == pytest.approx(100.0, abs=0.1)


# --------------------------------------------------------------------------
# resolve_target_allocation_json — persistence-time carry-forward fallback.
# A transient build failure must NOT silently persist NULL (the draft-36
# 422 regression): it carries forward the prior CURRENT plan's canonical doc.
# --------------------------------------------------------------------------

class _PriorPlan:
    def __init__(self, taj: str | None, pid: int = 99) -> None:
        self.id = pid
        self.target_allocation_json = taj


def _patch_resolve(monkeypatch, *, build_result, prior_plan):
    """Patch the helper's two dependencies: the fresh build + prior-current lookup."""
    import argosy.services.target_allocation_doc as tad
    import argosy.state.queries as queries

    def _build(*_a, **_k):
        if isinstance(build_result, Exception):
            raise build_result
        return build_result

    monkeypatch.setattr(tad, "build_plan_target_allocation_doc", _build)
    monkeypatch.setattr(queries, "get_current_plan", lambda *_a, **_k: prior_plan)


def test_resolve_returns_fresh_build_json(monkeypatch) -> None:
    from argosy.services.target_allocation_doc import resolve_target_allocation_json

    doc = build_target_allocation_doc(today=date(2026, 6, 12), today_composition=_TODAY_FULL_BOOK)
    _patch_resolve(monkeypatch, build_result=doc, prior_plan=_PriorPlan("STALE"))
    out = resolve_target_allocation_json(None, "ariel", 96, date(2026, 6, 12))
    # Fresh build wins — the prior doc is NOT consulted.
    assert out == doc.model_dump_json()


def test_resolve_carries_forward_when_build_none(monkeypatch) -> None:
    from argosy.services.target_allocation_doc import resolve_target_allocation_json

    _patch_resolve(monkeypatch, build_result=None, prior_plan=_PriorPlan('{"prior":"doc"}'))
    out = resolve_target_allocation_json(None, "ariel", 96, date(2026, 6, 12))
    assert out == '{"prior":"doc"}'


def test_resolve_stamps_provenance_on_valid_carried_doc(monkeypatch) -> None:
    from argosy.services.target_allocation_doc import resolve_target_allocation_json

    prior_doc = build_target_allocation_doc(
        today=date(2026, 6, 12), today_composition=_TODAY_FULL_BOOK
    )
    _patch_resolve(
        monkeypatch, build_result=None, prior_plan=_PriorPlan(prior_doc.model_dump_json(), pid=42)
    )
    out = resolve_target_allocation_json(None, "ariel", 96, date(2026, 6, 12))
    assert out is not None
    carried = TargetAllocationDoc.model_validate_json(out)
    # Cap + classes are preserved; provenance is marked carried-forward so the
    # doc is never mistaken for a fresh same-run build (codex r2 B3).
    assert carried.nvda_cap_pct == prior_doc.nvda_cap_pct
    assert "CARRIED-FORWARD" in carried.provenance
    assert "42" in carried.provenance


def test_resolve_carries_forward_when_build_raises(monkeypatch) -> None:
    from argosy.services.target_allocation_doc import resolve_target_allocation_json

    _patch_resolve(
        monkeypatch, build_result=RuntimeError("transient"), prior_plan=_PriorPlan('{"prior":"doc"}')
    )
    out = resolve_target_allocation_json(None, "ariel", 96, date(2026, 6, 12))
    assert out == '{"prior":"doc"}'


def _prior_doc_with_alternatives() -> TargetAllocationDoc:
    return TargetAllocationDoc(
        anchor_sigma=0.18, blended_sigma=0.18, nvda_cap_pct=13.0, fi_pct=20.0,
        provenance="prior CURRENT plan", glide=[],
        classes=[
            AllocationClassDoc(
                label="Alternatives", snapshot_category="Alternative",
                sigma_class="alternatives", target_pct=3.0,
                instruments=[AllocationInstrument(
                    symbol="BITC", role="primary", weight_within_class_pct=100.0,
                    domicile="JE")],
            ),
            AllocationClassDoc(
                label="Cash & T-bills (incl. ILS tranche)", snapshot_category="Cash",
                sigma_class="cash", target_pct=20.0, instruments=[]),
        ],
    )


def test_carry_forward_strips_stale_alternatives_sleeve(monkeypatch) -> None:
    # codex risk #6: a failed fresh build/verification must NOT silently preserve
    # a stale team-sourced alternatives sleeve (dynamic, unverified-this-run).
    from argosy.services.target_allocation_doc import resolve_target_allocation_json

    prior = _prior_doc_with_alternatives()
    _patch_resolve(monkeypatch, build_result=None,
                   prior_plan=_PriorPlan(prior.model_dump_json(), pid=7))
    out = resolve_target_allocation_json(None, "ariel", 96, date(2026, 6, 12))
    carried = TargetAllocationDoc.model_validate_json(out)
    # No alternatives class survives.
    assert not any(c.sigma_class == "alternatives" for c in carried.classes)
    assert not any(
        i.symbol == "BITC" for c in carried.classes for i in c.instruments
    )
    # Provenance records the drop, and the stale 3% is folded into cash so the
    # doc still anchors coherently (20% + 3% = 23%).
    assert "alternatives" in carried.provenance.lower()
    cash = next(c for c in carried.classes if c.sigma_class == "cash")
    assert cash.target_pct == pytest.approx(23.0)


def _approved_sleeve():
    from argosy.services.alternatives_types import (
        AlternativesSleeveDecision, VerificationEvidence, VerificationResult,
        VerifiedAlternativesCandidate,
    )
    cand = VerifiedAlternativesCandidate(
        symbol="SGLD", name="Invesco Physical Gold ETC", asset_class="precious_metals",
        domicile="IE", isin="IE00B579F325", weight_within_sleeve_pct=100.0,
        conviction="HIGH", thesis_md="gold",
        verification=VerificationResult(
            symbol="SGLD", verified=True, severity="GREEN", reason="ok",
            evidence=VerificationEvidence(isin_checksum_ok=True, isin_prefix="IE",
                                          domicile_coherent=True, registry_hit=True),
            resolved_isin="IE00B579F325", resolved_domicile="IE"),
    )
    return AlternativesSleeveDecision(
        target_pct=3.0, sleeve_sigma=0.16, instruments=[cand], decision="approve",
        rationale_md="team-sourced gold sleeve",
    )


def test_doc_includes_team_sourced_alternatives_when_supplied() -> None:
    doc = build_target_allocation_doc(
        today=date(2026, 6, 12), today_composition=_TODAY_FULL_BOOK,
        alternatives_sleeve=_approved_sleeve(),
    )
    alt = next((c for c in doc.classes if c.sigma_class == "alternatives"), None)
    assert alt is not None
    assert alt.target_pct == pytest.approx(3.0, abs=0.02)
    assert any(i.symbol == "SGLD" for i in alt.instruments)


def test_doc_has_no_alternatives_without_supplied_sleeve() -> None:
    doc = build_target_allocation_doc(
        today=date(2026, 6, 12), today_composition=_TODAY_FULL_BOOK
    )
    assert not any(c.sigma_class == "alternatives" for c in doc.classes)


def test_carry_forward_without_alternatives_is_unchanged_except_provenance(monkeypatch) -> None:
    from argosy.services.target_allocation_doc import resolve_target_allocation_json

    prior = build_target_allocation_doc(
        today=date(2026, 6, 12), today_composition=_TODAY_FULL_BOOK
    )
    assert not any(c.sigma_class == "alternatives" for c in prior.classes)
    _patch_resolve(monkeypatch, build_result=None,
                   prior_plan=_PriorPlan(prior.model_dump_json(), pid=8))
    out = resolve_target_allocation_json(None, "ariel", 96, date(2026, 6, 12))
    carried = TargetAllocationDoc.model_validate_json(out)
    # Non-alternatives classes carry forward intact.
    assert {c.label for c in carried.classes} == {c.label for c in prior.classes}
    assert "CARRIED-FORWARD" in carried.provenance


def test_resolve_returns_none_when_no_fresh_and_no_prior(monkeypatch) -> None:
    from argosy.services.target_allocation_doc import resolve_target_allocation_json

    # No fresh build AND no prior current plan (first-ever plan, un-anchored).
    _patch_resolve(monkeypatch, build_result=None, prior_plan=None)
    out = resolve_target_allocation_json(None, "ariel", 96, date(2026, 6, 12))
    assert out is None

    # Prior exists but has no doc → still None (nothing to carry forward).
    _patch_resolve(monkeypatch, build_result=None, prior_plan=_PriorPlan(None))
    assert resolve_target_allocation_json(None, "ariel", 96, date(2026, 6, 12)) is None
