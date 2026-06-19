from argosy.quality.figure_registry import FigureKind, Materiality, OwnerRole
from argosy.quality.figure_registry import FigureRecord
from argosy.quality.figure_registry import OWNER_MAP, OwnerSpec, owner_for
from argosy.quality.figure_registry import validate_figure
from argosy.quality.figure_registry import build_figure_registry
from argosy.services.plan_numeric_resolver import ResolvedPlanNumbers, ResolvedValue


def test_enums_have_expected_members():
    assert FigureKind.FORMULA_RESULT.value == "formula_result"
    assert {k.value for k in FigureKind} == {
        "source_fact", "assumption", "formula_result",
        "model_projection", "interpretation", "recommendation",
    }
    assert {m.value for m in Materiality} == {"high", "medium", "low"}
    assert OwnerRole.RETIREMENT_FI.value == "retirement_fi"
    assert OwnerRole.LEAD_PLANNER.value == "lead_planner"


def test_figure_record_carries_full_identity_and_defaults():
    r = FigureRecord(
        id="retirement.fi_target_nis", value=11_836_133.0, unit="nis",
        kind=FigureKind.FORMULA_RESULT, owner=OwnerRole.RETIREMENT_FI,
    )
    assert r.basis is None and r.scenario is None and r.as_of is None
    assert r.materiality is Materiality.MEDIUM
    assert r.consult == () and r.evidence == ()
    assert r.validated_by == "none"
    assert r.status == "pending"
    assert r.version == 0 and r.timestamp is None


def test_explicit_owner_map_entry():
    spec = owner_for("retirement.fi_target_nis")
    assert spec.owner is OwnerRole.RETIREMENT_FI
    assert spec.kind is FigureKind.FORMULA_RESULT
    assert spec.materiality is Materiality.HIGH


def test_shared_concept_has_consult_set():
    spec = owner_for("concentration.nvda_cap_pct")
    assert spec.owner is OwnerRole.INVESTMENT
    assert OwnerRole.TAX in spec.consult


def test_prefix_rules_cover_dynamic_and_canonical_keys():
    # dynamic allocation.* and the canonical-path keys resolve by prefix rule.
    assert owner_for("allocation.global_equity").owner is OwnerRole.INVESTMENT
    assert owner_for("fx.usd_nis_band_low").owner is OwnerRole.BALANCE_SHEET
    assert owner_for("statutory.retirement_age").owner is OwnerRole.RETIREMENT_FI
    assert owner_for("mc.solvency_horizon_age").owner is OwnerRole.RETIREMENT_FI
    assert owner_for("spend.mc_central_nis").owner is OwnerRole.CASH_FLOW


def test_unknown_key_is_flagged_not_crashed():
    spec = owner_for("totally.unknown_key")
    assert spec.owner is OwnerRole.LEAD_PLANNER  # safe fallback
    assert spec.uncategorized is True


def _rec(**kw):
    # evidence present by default — every publishable figure must carry it
    # (codex impl review #1); no-evidence cases pass evidence=() explicitly.
    base = dict(id="x", value=1.0, unit="nis", evidence=("src:1",),
                kind=FigureKind.FORMULA_RESULT, owner=OwnerRole.RETIREMENT_FI)
    base.update(kw)
    return FigureRecord(**base)


def test_formula_result_resolves_on_resolver_or_recompute():
    assert validate_figure(_rec(validated_by="resolver")).status == "resolved"
    assert validate_figure(_rec(validated_by="recompute")).status == "resolved"


def test_formula_result_pending_without_marker():
    assert validate_figure(_rec(validated_by="none")).status == "pending"


def test_source_fact_resolves_on_resolver():
    out = validate_figure(_rec(kind=FigureKind.SOURCE_FACT, validated_by="resolver"))
    assert out.status == "resolved"


def test_material_judgment_no_evidence_is_blocked():
    out = validate_figure(_rec(kind=FigureKind.RECOMMENDATION,
                               materiality=Materiality.HIGH, evidence=()))
    assert out.status == "blocked"


def test_material_judgment_with_evidence_no_cross_model_is_pending():
    for mat in (Materiality.HIGH, Materiality.MEDIUM):
        out = validate_figure(_rec(kind=FigureKind.MODEL_PROJECTION,
                                   materiality=mat, evidence=("src:1",)))
        assert out.status == "pending", mat


def test_material_judgment_with_cross_model_resolves():
    out = validate_figure(_rec(kind=FigureKind.RECOMMENDATION,
                               materiality=Materiality.HIGH, evidence=("src:1",),
                               validated_by="cross_model_rederivation"))
    assert out.status == "resolved"


def test_low_materiality_judgment_resolves_on_evidence():
    out = validate_figure(_rec(kind=FigureKind.ASSUMPTION,
                               materiality=Materiality.LOW, evidence=("src:1",)))
    assert out.status == "resolved"


def test_low_materiality_judgment_no_evidence_is_blocked():
    out = validate_figure(_rec(kind=FigureKind.ASSUMPTION,
                               materiality=Materiality.LOW, evidence=()))
    assert out.status == "blocked"


def test_none_value_stays_pending():
    assert validate_figure(_rec(value=None, validated_by="resolver")).status == "pending"


# --- codex impl-review fixes (ZigZag) ---

def test_deterministic_no_evidence_is_blocked():
    """A formula_result with no evidence must not resolve even when cleared
    (codex #1)."""
    out = validate_figure(_rec(validated_by="resolver", evidence=()))
    assert out.status == "blocked"


def test_uncategorized_figure_is_blocked_fail_closed():
    """An un-owned (uncategorized) figure can never ship (codex #2)."""
    out = validate_figure(_rec(validated_by="resolver", uncategorized=True))
    assert out.status == "blocked"


def test_non_finite_value_stays_pending():
    """nan/inf are not publishable (codex #4)."""
    import math
    assert validate_figure(_rec(value=math.nan, validated_by="resolver")).status == "pending"
    assert validate_figure(_rec(value=math.inf, validated_by="resolver")).status == "pending"


def test_string_claim_value_is_publishable():
    """A non-numeric claim string is a valid value (e.g. a 'suitable' assertion)."""
    out = validate_figure(_rec(value="suitable", kind=FigureKind.RECOMMENDATION,
                               materiality=Materiality.LOW, evidence=("src:1",)))
    assert out.status == "resolved"


def test_materiality_robust_to_string_hydration():
    """materiality passed as a plain str (JSON hydration) is normalized so the
    LOW branch still fires (codex #5)."""
    out = validate_figure(_rec(kind=FigureKind.ASSUMPTION, materiality="low",
                               evidence=("src:1",)))
    assert out.status == "resolved"


def test_frozen_record_coerces_mutable_sequences_to_tuple():
    """A list passed for evidence/consult is coerced to a tuple so a 'frozen'
    record can't be mutated through it (codex #7)."""
    r = FigureRecord(id="x", value=1.0, unit="nis", kind=FigureKind.FORMULA_RESULT,
                     owner=OwnerRole.RETIREMENT_FI, evidence=["a", "b"], consult=[OwnerRole.TAX])
    assert isinstance(r.evidence, tuple) and r.evidence == ("a", "b")
    assert isinstance(r.consult, tuple)


def test_estate_keys_owned_by_estate_not_investment():
    """Estate-tax figures must route to Estate, not be caught by the broad
    concentration./fallback rules (codex #3)."""
    assert owner_for("concentration.us_situs_estate_nis").owner is OwnerRole.ESTATE
    assert owner_for("estate.us_situs_exposure_nis").owner is OwnerRole.ESTATE


def test_build_registry_wraps_with_ownership_and_resolves_formula():
    man = ResolvedPlanNumbers(values={
        "retirement.fi_target_nis": ResolvedValue(
            key="retirement.fi_target_nis", value=11_836_133.0, unit="nis",
            status="resolved", source_locator="fi_methodology", formula="spend/SWR"),
        "concentration.nvda_cap_pct": ResolvedValue(
            key="concentration.nvda_cap_pct", value=0.13, unit="pct",
            status="resolved", source_locator="target_allocation_doc"),
    })
    reg = build_figure_registry(man, today="2026-06-19")
    fi = reg["retirement.fi_target_nis"]
    assert fi.owner is OwnerRole.RETIREMENT_FI and fi.kind is FigureKind.FORMULA_RESULT
    assert fi.value == 11_836_133.0 and fi.evidence == ("fi_methodology",)
    assert fi.method == "spend/SWR" and fi.as_of == "2026-06-19"
    assert fi.validated_by == "resolver" and fi.status == "resolved"
    assert OwnerRole.TAX in reg["concentration.nvda_cap_pct"].consult


def test_build_registry_material_projection_is_pending():
    man = ResolvedPlanNumbers(values={
        "retirement.earliest_safe_age": ResolvedValue(
            key="retirement.earliest_safe_age", value=46.0, unit="age",
            status="resolved", source_locator="canonical_dual_track"),
    })
    rec = build_figure_registry(man)["retirement.earliest_safe_age"]
    assert rec.kind is FigureKind.MODEL_PROJECTION and rec.materiality is Materiality.HIGH
    assert rec.status == "pending"  # awaits Phase-3 cross-model validation


def test_build_registry_pending_value_stays_pending():
    man = ResolvedPlanNumbers(values={
        "retirement.fi_target_nis": ResolvedValue.pending(
            "retirement.fi_target_nis", "nis", "no source"),
    })
    assert build_figure_registry(man)["retirement.fi_target_nis"].status == "pending"


def test_build_registry_dynamic_allocation_key_is_owned():
    man = ResolvedPlanNumbers(values={
        "allocation.global_equity": ResolvedValue(
            key="allocation.global_equity", value=0.35, unit="pct",
            status="resolved", source_locator="target_allocation_doc"),
    })
    rec = build_figure_registry(man)["allocation.global_equity"]
    assert rec.owner is OwnerRole.INVESTMENT and rec.status == "resolved"


import pytest


def test_static_owner_map_keys_are_all_categorized():
    # every explicit key resolves to itself (not the uncategorized fallback)
    for key in OWNER_MAP:
        assert owner_for(key).uncategorized is False


def test_synth_display_keys_are_owned():
    from argosy.services.plan_numeric_resolver import _SYNTH_DISPLAY
    bad = [k for (k, _label) in _SYNTH_DISPLAY if owner_for(k).uncategorized]
    assert bad == [], f"_SYNTH_DISPLAY keys with no owner: {bad}"


def test_live_resolver_keys_all_owned_and_no_blocked_formula():
    """Run the real resolver; every produced key must be owned (not uncategorized),
    and no formula_result/source_fact may be 'blocked'. Material judgments may be
    'pending' (awaiting Phase-3 cross-model). Skips if the dev DB is absent."""
    import os
    os.environ["ARGOSY_INCREMENTAL_PLAN"] = "1"
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from argosy.config import get_settings
        from argosy.services.plan_numeric_resolver import resolve_plan_numbers
    except Exception:
        pytest.skip("resolver deps unavailable")
    url = get_settings().database_url.replace("+aiosqlite", "")
    try:
        S = sessionmaker(bind=create_engine(url, connect_args={"check_same_thread": False}))
        with S() as s:
            man = resolve_plan_numbers(s, user_id="ariel", decision_run_id=117,
                                       include_canonical_ages=True)
    except Exception:
        pytest.skip("dev DB / run 117 unavailable")
    reg = build_figure_registry(man)
    uncategorized = sorted(k for k, r in reg.items()
                           if owner_for(k).uncategorized)
    assert uncategorized == [], f"un-owned resolver keys: {uncategorized}"
    blocked_formula = sorted(
        k for k, r in reg.items()
        if r.status == "blocked" and r.kind in (FigureKind.FORMULA_RESULT, FigureKind.SOURCE_FACT))
    assert blocked_formula == [], f"deterministic figures blocked: {blocked_formula}"


def test_total_net_worth_basis_owned_and_labeled():
    spec = owner_for("portfolio.total_net_worth_incl_residence_nis")
    assert spec.owner is OwnerRole.BALANCE_SHEET
    assert spec.basis == "total"
    assert spec.uncategorized is False
