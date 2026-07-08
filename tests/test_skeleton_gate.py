"""Skeleton gate units — sliced full synthesis stage A.

Design: docs/design/sliced_full_synthesis.md §5 (skeleton gate units):
manifest mismatch fails; ``[derivation pending]`` passes; corrective
wrong-value present fails; ``no_change`` + non-empty deltas fails;
coverage floor; speculation constraints. Pure — no DB, no LLM.
"""

from __future__ import annotations

from datetime import date

from argosy.agents.plan_skeleton_synthesizer import (
    PlanSkeleton,
    SkeletonDelta,
    SkeletonHorizon,
    SkeletonSectionEntry,
)
from argosy.agents.plan_synthesizer_types import (
    SpeculativeCandidate,
    SynthTarget,
)
from argosy.quality.skeleton_gate import check_skeleton
from argosy.services.plan_numeric_resolver import (
    ResolvedPlanNumbers,
    ResolvedValue,
)

_CANONICAL_12 = [
    "cover_assumptions", "client_goals", "net_worth", "cashflow",
    "capital_sufficiency", "ips", "concentration", "withdrawal",
    "monte_carlo", "tax_plan", "estate", "action_items",
]


def _horizon(name, *, status="minor_revision", targets=None, cands=None):
    fresh = {"long": "annual", "medium": "quarterly", "short": "monthly"}
    return SkeletonHorizon(
        horizon=name,
        freshness_expected=fresh[name],
        status=status,
        posture_summary=f"{name} stance.",
        targets=targets or [],
        speculative_candidates=cands or [],
    )


def _target(label, value, unit="pct_of_portfolio"):
    return SynthTarget(
        label=label, value=value, unit=unit,
        stated_at=date(2026, 7, 8), revisit_after=date(2027, 7, 8),
    )


def _skeleton(**kw):
    defaults = dict(
        long=_horizon("long", status="no_change"),
        medium=_horizon("medium"),
        short=_horizon("short"),
        delta_roster=[],
        section_roster=[
            SkeletonSectionEntry(
                section_id=sid, horizon="medium",
                one_line_thesis=f"{sid} thesis",
            )
            for sid in _CANONICAL_12
        ],
    )
    defaults.update(kw)
    return PlanSkeleton(**defaults)


def _manifest(**values):
    resolved = {}
    for key, (value, unit) in values.items():
        resolved[key] = ResolvedValue(
            key=key, value=value, unit=unit, status="resolved",
            source_locator="test",
        )
    return ResolvedPlanNumbers(values=resolved)


# ----------------------------------------------------------------------
# Check 1 — manifest floor
# ----------------------------------------------------------------------


def test_headline_target_matching_manifest_passes():
    manifest = _manifest(**{"concentration.nvda_target_pct": (8.0, "pct")})
    sk = _skeleton(medium=_horizon(
        "medium", targets=[_target("NVDA target weight", 8.0)],
    ))
    res = check_skeleton(skeleton=sk, resolved=manifest)
    assert res.passes, res.violations


def test_headline_target_manifest_mismatch_fails():
    manifest = _manifest(**{"concentration.nvda_target_pct": (8.0, "pct")})
    sk = _skeleton(medium=_horizon(
        "medium", targets=[_target("NVDA target weight", 12.0)],
    ))
    res = check_skeleton(skeleton=sk, resolved=manifest)
    assert not res.passes
    assert any("manifest" in v and "NVDA target weight" in v
               for v in res.violations)


def test_headline_target_matches_fraction_form_manifest():
    """Resolver pct values may be stored as fractions (0.08 == 8%) — the
    gate mirrors numeric_source_gate._traces and checks both forms
    (codex sliced review blocker #2)."""
    manifest = _manifest(**{"concentration.nvda_target_pct": (0.08, "pct")})
    sk = _skeleton(medium=_horizon(
        "medium", targets=[_target("NVDA target weight", 8.0)],
    ))
    res = check_skeleton(skeleton=sk, resolved=manifest)
    assert res.passes, res.violations
    # A genuinely wrong percent still fails against the fraction form.
    sk_bad = _skeleton(medium=_horizon(
        "medium", targets=[_target("NVDA target weight", 12.0)],
    ))
    assert not check_skeleton(skeleton=sk_bad, resolved=manifest).passes


def test_pct_dual_form_never_accepts_100x_wrong_value():
    """codex r2: the *100 form applies only to fraction-looking resolved
    values — a percent-points manifest entry (8.0) must never accept a
    100x-wrong target (800.0)."""
    manifest = _manifest(**{"concentration.nvda_target_pct": (8.0, "pct")})
    sk = _skeleton(medium=_horizon(
        "medium", targets=[_target("NVDA target weight", 800.0)],
    ))
    assert not check_skeleton(skeleton=sk, resolved=manifest).passes


def test_non_headline_target_is_left_alone():
    manifest = _manifest(**{"concentration.nvda_target_pct": (8.0, "pct")})
    sk = _skeleton(medium=_horizon(
        "medium", targets=[_target("EM diversifier sleeve", 12.0)],
    ))
    assert check_skeleton(skeleton=sk, resolved=manifest).passes


def test_derivation_pending_key_fact_passes():
    manifest = _manifest(**{"retirement.fi_target_nis": (17_000_000.0, "nis")})
    sk = _skeleton(section_roster=[
        SkeletonSectionEntry(
            section_id=sid, horizon="medium", one_line_thesis=f"{sid} t",
            key_facts=(
                ["FI capital target: [derivation pending]"]
                if sid == "capital_sufficiency" else []
            ),
        )
        for sid in _CANONICAL_12
    ])
    res = check_skeleton(skeleton=sk, resolved=manifest)
    assert res.passes, res.violations


def test_fabricated_key_fact_headline_fails():
    manifest = _manifest(**{"retirement.fi_target_nis": (17_000_000.0, "nis")})
    sk = _skeleton(section_roster=[
        SkeletonSectionEntry(
            section_id=sid, horizon="medium", one_line_thesis=f"{sid} t",
            key_facts=(
                ["FI capital target is ₪21.00M"]
                if sid == "capital_sufficiency" else []
            ),
        )
        for sid in _CANONICAL_12
    ])
    res = check_skeleton(skeleton=sk, resolved=manifest)
    assert not res.passes
    assert any("key_facts" in v for v in res.violations)


def test_no_manifest_skips_headline_check():
    sk = _skeleton(medium=_horizon(
        "medium", targets=[_target("NVDA target weight", 12.0)],
    ))
    assert check_skeleton(skeleton=sk, resolved=None).passes


# ----------------------------------------------------------------------
# Check 2 — corrective values
# ----------------------------------------------------------------------


def test_corrective_wrong_value_present_fails():
    sk = _skeleton(medium=_horizon(
        "medium", targets=[_target("FX planning rate", 3.00, unit="ratio")],
    ))
    res = check_skeleton(
        skeleton=sk,
        corrections=[{
            "index": 1, "topic": "fx-rate",
            "canonical_values": [], "wrong_values": ["3.00"],
        }],
    )
    # 3.00 renders as "3.0" in the model dump; the variant widening finds it.
    assert not res.passes
    assert any("wrong value" in v for v in res.violations)


def test_corrective_canonical_absent_fails_and_present_passes():
    correction = {
        "index": 2, "topic": "glide",
        "canonical_values": [4136], "wrong_values": [9999],
    }
    sk_missing = _skeleton()
    res = check_skeleton(skeleton=sk_missing, corrections=[correction])
    assert not res.passes
    assert any("canonical" in v for v in res.violations)

    sk_present = _skeleton(medium=_horizon(
        "medium",
        targets=[_target("NVDA 2026 sale tranche", 4136, unit="shares")],
    ))
    assert check_skeleton(skeleton=sk_present, corrections=[correction]).passes


def test_directive_superseded_value_present_fails():
    sk = _skeleton(medium=_horizon(
        "medium",
        targets=[_target("NVDA 2026 sale tranche", 9880, unit="shares")],
    ))
    res = check_skeleton(
        skeleton=sk,
        directives=[{"index": 1, "wrong_values": [9880]}],
    )
    assert not res.passes
    assert any("superseded" in v for v in res.violations)


# ----------------------------------------------------------------------
# Check 3 — delta roster
# ----------------------------------------------------------------------


def test_no_change_with_deltas_fails():
    sk = _skeleton(delta_roster=[SkeletonDelta(
        item_kind="target", item_id="long.targets.swr", horizon="long",
        change_kind="modified", summary="nudge",
    )])
    res = check_skeleton(skeleton=sk)
    assert not res.passes
    assert any("no_change" in v for v in res.violations)


def test_delta_id_resolution():
    prior = {"medium.targets.nvda"}
    ok = _skeleton(delta_roster=[
        SkeletonDelta(item_kind="target", item_id="medium.targets.nvda",
                      horizon="medium", change_kind="modified", summary="s"),
        SkeletonDelta(item_kind="theme", item_id="medium.themes.new_tilt",
                      horizon="medium", change_kind="added", summary="s"),
    ])
    assert check_skeleton(skeleton=ok, prior_item_ids=prior).passes

    bad_removed = _skeleton(delta_roster=[SkeletonDelta(
        item_kind="target", item_id="medium.targets.never_existed",
        horizon="medium", change_kind="removed", summary="s",
    )])
    res = check_skeleton(skeleton=bad_removed, prior_item_ids=prior)
    assert not res.passes

    wrong_horizon = _skeleton(delta_roster=[SkeletonDelta(
        item_kind="target", item_id="short.targets.x", horizon="medium",
        change_kind="added", summary="s",
    )])
    res = check_skeleton(skeleton=wrong_horizon, prior_item_ids=prior)
    assert not res.passes
    assert any("horizon" in v for v in res.violations)

    malformed = _skeleton(delta_roster=[SkeletonDelta(
        item_kind="target", item_id="nonsense id!!", horizon="medium",
        change_kind="added", summary="s",
    )])
    assert not check_skeleton(skeleton=malformed, prior_item_ids=prior).passes


# ----------------------------------------------------------------------
# Check 4 — coverage + speculation
# ----------------------------------------------------------------------


def test_coverage_floor():
    sk = _skeleton(section_roster=[SkeletonSectionEntry(
        section_id="ips", horizon="medium", one_line_thesis="t",
    )])
    res = check_skeleton(skeleton=sk)
    assert not res.passes
    assert any("coverage" in v for v in res.violations)
    assert check_skeleton(skeleton=sk, coverage_floor=1).passes


def _cand(pct=0.001, ceiling=True):
    return SpeculativeCandidate(
        ticker="XYZ", thesis_summary="t", suggested_position_usd=1000,
        suggested_position_pct_of_net_worth=pct, risk_ceiling_check=ceiling,
        horizon_days=30, expected_drawdown_pct=50, exit_trigger="stop",
    )


def test_speculation_short_only_and_cap():
    on_medium = _skeleton(medium=_horizon("medium", cands=[_cand()]))
    res = check_skeleton(skeleton=on_medium)
    assert any("short-horizon only" in v for v in res.violations)

    over_cap = _skeleton(short=_horizon("short", cands=[_cand(pct=0.5)]))
    res = check_skeleton(skeleton=over_cap, speculation_cap_pct=0.001)
    assert any("exceeds the cap" in v for v in res.violations)

    too_many = _skeleton(short=_horizon(
        "short", cands=[_cand(), _cand(), _cand(), _cand()],
    ))
    res = check_skeleton(skeleton=too_many, speculation_cap_concurrent=3)
    assert any("concurrent" in v for v in res.violations)

    no_check = _skeleton(short=_horizon("short", cands=[_cand(ceiling=False)]))
    res = check_skeleton(skeleton=no_check, speculation_cap_pct=0.001)
    assert any("risk_ceiling_check" in v for v in res.violations)

    within = _skeleton(short=_horizon("short", cands=[_cand()]))
    assert check_skeleton(
        skeleton=within, speculation_cap_pct=0.001,
        speculation_cap_concurrent=3,
    ).passes
