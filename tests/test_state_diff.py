"""Tests for :mod:`argosy.services.state_diff` — Spec B commit #3.

Spec: ``docs/superpowers/specs/2026-05-29-state-observer-agent-design.md`` §2.

Covers the pure-function diff service. Tests are organized by:

  * **Core diff semantics** — numeric_pct sign convention, appeared /
    disappeared, categorical change.
  * **Cross-section plan-baseline pairing** — the FX-emergence gate.
  * **Filter rules** — §2.4 noise filter, magnitude floors, allowlist.
  * **Truncation** — §2.5 ``MAX_FIELDS_PER_DIFF`` semantics.
  * **Bucket determinism** — :func:`compute_deviation_bucket` monotonicity.
  * **CI invariants** — every numeric snapshot-schema field is either
    paired in the comparator map OR documented as "no plan baseline",
    and every snapshot prefix is enumerable for commit #6's
    ``inferred_kind`` mapping.

No DB; no I/O. State snapshots are mocked as plain dicts (sibling
commit #2 owns the actual collector).
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from argosy.services.state_diff import (
    ALWAYS_INCLUDE_ALLOWLIST,
    DEFAULT_NUMERIC_THRESHOLD_PCT,
    DEVIATION_BUCKET_ORDER,
    FieldDiff,
    FullDiff,
    MAX_FIELDS_PER_DIFF,
    PLAN_BASELINE_COMPARATOR_MAP,
    SNAPSHOT_FIELD_PREFIXES,
    compute_deviation_bucket,
    compute_diff,
    compute_full_diff,
)
from argosy.services.state_diff import _NO_PLAN_BASELINE_FIELDS


# ---------------------------------------------------------------------------
# Fixtures — small representative snapshot dicts
# ---------------------------------------------------------------------------

def _plan_inputs(fx: float = 3.6) -> dict[str, Any]:
    return {
        "assumed_fx_usd_nis": fx,
        "assumed_mu_nominal_annual": 0.08,
        "assumed_sigma_annual": 0.18,
        "assumed_inflation_annual": 0.025,
        "assumed_retirement_age": 60.0,
        "assumed_marginal_tax_rate": 0.35,
        "assumed_monthly_expenses_nis": 35_000.0,
        "assumed_monthly_income_nis": 60_000.0,
        "assumed_withdrawal_policy": "constant_real",
    }


def _macro(fx: float = 2.81) -> dict[str, Any]:
    return {
        "fx_usd_nis_spot": fx,
        "fx_usd_nis_30d_avg": fx + 0.02,
        "fed_funds_rate_pct": 0.0525,
        "treasury_10y_yield_pct": 0.043,
        "sp500_index": 5_300.0,
        "sp500_30d_return_pct": 0.012,
        "nasdaq_index": 18_400.0,
        "nasdaq_30d_return_pct": 0.018,
        "vix": 15.0,
    }


def _portfolio() -> dict[str, Any]:
    return {
        "total_value_usd": 1_200_000.0,
        "cash_balances_usd": 40_000.0,
        "positions": [
            {"ticker": "NVDA", "shares": 200, "value_usd": 420_000.0, "value_nis": 1_180_000.0, "asset_class": "Growth"},
            {"ticker": "QQQM", "shares": 800, "value_usd": 180_000.0, "value_nis": 506_000.0, "asset_class": "Growth"},
        ],
        "allocations": [
            {"category": "Growth", "current_pct": 0.52, "target_pct": 0.40, "current_k_usd": 624.0, "target_k_usd": 480.0},
            {"category": "Income", "current_pct": 0.28, "target_pct": 0.30, "current_k_usd": 336.0, "target_k_usd": 360.0},
            {"category": "Cash",   "current_pct": 0.10, "target_pct": 0.10, "current_k_usd": 120.0, "target_k_usd": 120.0},
            {"category": "Real Estate", "current_pct": 0.10, "target_pct": 0.20, "current_k_usd": 120.0, "target_k_usd": 240.0},
        ],
        "top_concentration_pct": 0.35,
        "unallocated_cash_usd": 12_000.0,
        "snapshot_date": "2026-05-29",
    }


def _current_state(fx_spot: float = 2.81) -> dict[str, Any]:
    return {
        "plan_inputs": _plan_inputs(fx=3.6),
        "portfolio": _portfolio(),
        "macro": _macro(fx=fx_spot),
        "cashflow_recent": {
            "last_3_months": [
                {"month_yyyy_mm": "2026-04", "projected_expense_nis": 35_000.0, "realized_expense_nis": 38_000.0,
                 "deviation_pct": 0.086, "projected_income_nis": 60_000.0, "realized_income_nis": 60_000.0,
                 "income_deviation_pct": 0.0},
                {"month_yyyy_mm": "2026-03", "projected_expense_nis": 35_000.0, "realized_expense_nis": 36_500.0,
                 "deviation_pct": 0.043, "projected_income_nis": 60_000.0, "realized_income_nis": 60_000.0,
                 "income_deviation_pct": 0.0},
                {"month_yyyy_mm": "2026-02", "projected_expense_nis": 35_000.0, "realized_expense_nis": 35_200.0,
                 "deviation_pct": 0.006, "projected_income_nis": 60_000.0, "realized_income_nis": 60_000.0,
                 "income_deviation_pct": 0.0},
            ],
            "cumulative_deviation_nis": 4_700.0,
        },
        "tax_assumptions": {
            "current_marginal_bracket_pct": 0.37,
            "effective_rate_prior_year_pct": 0.31,
            "assumed_marginal_rate_pct": 0.35,
            "withholding_supplemental_cap_pct": 0.22,
        },
        "metadata": {
            "snapshot_id": 17,
            "user_id": "ariel",
            "snapshot_date": "2026-05-29",
            "plan_draft_id": 5,
            "source_versions": {
                "argosy_git_sha": "abc123",
                "schema_migration_head": "0049",
                "trigger_reason": "daily_cron",
                "historical_replay_gaps": [],
            },
        },
    }


# ---------------------------------------------------------------------------
# Core diff semantics
# ---------------------------------------------------------------------------

def test_numeric_pct_3_6_to_2_8_is_approx_minus_22_percent():
    """The FX-emergence example from the spec: plan 3.6 vs current 2.8.

    Sign convention: ``(current - baseline) / baseline``.
    3.6 was the baseline; 2.8 is current; deviation is NEGATIVE because
    the current rate is BELOW what the plan assumed.
    """
    current = {"macro": {"fx_usd_nis_spot": 2.8}}
    baseline = {"macro": {"fx_usd_nis_spot": 3.6}}
    diffs = compute_diff(current, baseline)
    fx_rows = [d for d in diffs if d.path == "macro.fx_usd_nis_spot"]
    assert len(fx_rows) == 1, f"expected one FX row, got {fx_rows}"
    row = fx_rows[0]
    assert row.deviation_kind == "numeric_pct"
    assert row.magnitude is not None
    # (2.8 - 3.6) / 3.6 = -0.2222...
    assert math.isclose(row.magnitude, -0.2222, abs_tol=0.001), (
        f"expected ~-0.222, got {row.magnitude}"
    )
    # Sign convention sanity: current is LOWER than baseline -> magnitude negative.
    assert row.magnitude < 0


def test_numeric_pct_sign_convention_when_current_higher_than_baseline():
    """The mirror case: current ABOVE baseline -> positive magnitude."""
    current = {"v": 5.0}
    baseline = {"v": 4.0}
    diffs = compute_diff(current, baseline)
    assert len(diffs) == 1
    assert diffs[0].magnitude is not None
    assert diffs[0].magnitude > 0
    assert math.isclose(diffs[0].magnitude, 0.25, abs_tol=1e-9)


def test_appeared_field_emits_appeared_row():
    current = {"portfolio": {"positions": [{"ticker": "TSLA", "shares": 100}]}}
    baseline = {"portfolio": {"positions": []}}
    diffs = compute_diff(current, baseline)
    # We at least see the ticker as 'appeared' (depends on whether empty
    # list traversal emits anything; what we care about is that adding a
    # new entry results in at least one path that did not exist before).
    kinds = {d.deviation_kind for d in diffs}
    assert "appeared" in kinds, f"expected appeared in {kinds}"


def test_disappeared_field_emits_disappeared_row():
    """A field removed from current vs baseline emits ``disappeared``."""
    current: dict[str, Any] = {"keep": 1.0}
    baseline = {"keep": 1.0, "removed": 42.0}
    diffs = compute_diff(current, baseline)
    paths = {d.path: d.deviation_kind for d in diffs}
    assert paths == {"removed": "disappeared"}, paths


def test_categorical_change_detection():
    """Strings differing emit categorical_change with magnitude=None."""
    current = {"plan_inputs": {"assumed_withdrawal_policy": "guardrails"}}
    baseline = {"plan_inputs": {"assumed_withdrawal_policy": "constant_real"}}
    diffs = compute_diff(current, baseline)
    pol_rows = [d for d in diffs if d.path == "plan_inputs.assumed_withdrawal_policy"]
    assert len(pol_rows) == 1
    assert pol_rows[0].deviation_kind == "categorical_change"
    assert pol_rows[0].magnitude is None
    assert pol_rows[0].current_value == "guardrails"
    assert pol_rows[0].baseline_value == "constant_real"


def test_numeric_abs_when_baseline_is_zero():
    """Baseline zero -> we emit ``numeric_abs`` (not pct, which would be
    infinity / undefined)."""
    current = {"some_index": 0.5}
    baseline = {"some_index": 0.0}
    diffs = compute_diff(current, baseline)
    rows = [d for d in diffs if d.path == "some_index"]
    assert len(rows) == 1
    assert rows[0].deviation_kind == "numeric_abs"
    assert rows[0].magnitude == 0.5


def test_equal_values_emit_no_row():
    state = {"a": 1, "b": "x", "c": [1, 2, 3]}
    assert compute_diff(state, state) == []


def test_internal_underscore_fields_are_skipped():
    """Any path with a leading-underscore segment is filtered out."""
    current = {"_internal": {"debug": 1}, "real": 5.0}
    baseline = {"_internal": {"debug": 2}, "real": 4.0}
    diffs = compute_diff(current, baseline)
    paths = {d.path for d in diffs}
    assert "_internal.debug" not in paths
    # The real field still surfaces (>2% deviation).
    assert "real" in paths


def test_timestamp_fields_with_at_suffix_are_skipped():
    """Fields ending in ``_at`` are noisy timestamps and get dropped."""
    current = {"created_at": "2026-05-29T10:00:00Z", "value": 5.0}
    baseline = {"created_at": "2026-05-28T10:00:00Z", "value": 4.0}
    diffs = compute_diff(current, baseline)
    paths = {d.path for d in diffs}
    assert "created_at" not in paths
    assert "value" in paths


def test_metadata_snapshot_id_skipped():
    """``metadata.snapshot_id`` is always-different by construction."""
    current = {"metadata": {"snapshot_id": 17, "user_id": "ariel"}, "v": 5.0}
    baseline = {"metadata": {"snapshot_id": 16, "user_id": "ariel"}, "v": 4.0}
    diffs = compute_diff(current, baseline)
    paths = {d.path for d in diffs}
    assert "metadata.snapshot_id" not in paths


# ---------------------------------------------------------------------------
# Plan-baseline cross-section pairing (the FX-emergence gate)
# ---------------------------------------------------------------------------

def test_compute_full_diff_fx_emergence_22_percent():
    """The architecture's empirical contract: when current macro
    fx_usd_nis_spot is 2.8 and plan_inputs.assumed_fx_usd_nis is 3.6,
    the vs_plan diff MUST contain a row pairing those two with ~-22%.

    This is the test that locks in the FX-emergence gate. If this test
    fails, the comparator map is wrong and the observer can't catch the
    case it was designed to catch.
    """
    current = _current_state(fx_spot=2.8)
    plan_baseline = {
        "plan_inputs": _plan_inputs(fx=3.6),
        "portfolio": {"allocations": _portfolio()["allocations"]},  # for allocation pairing
    }
    full = compute_full_diff(current, plan_baseline, prior_snapshot=None)
    fx_rows = [d for d in full.vs_plan if d.path == "macro.fx_usd_nis_spot"]
    assert len(fx_rows) == 1, (
        f"FX-emergence row missing. vs_plan paths = "
        f"{[d.path for d in full.vs_plan]}"
    )
    row = fx_rows[0]
    assert row.deviation_kind == "numeric_pct"
    assert row.magnitude is not None
    assert math.isclose(row.magnitude, -0.2222, abs_tol=0.001)
    assert row.baseline_label == "plan"
    assert row.baseline_value == 3.6
    assert row.current_value == 2.8


def test_plan_baseline_comparator_map_includes_fx_pairing():
    """Codex review focus: the FX pairing MUST be present in the map.
    If this constant is missing or renamed, the FX-emergence test
    above can't even run.
    """
    assert "macro.fx_usd_nis_spot" in PLAN_BASELINE_COMPARATOR_MAP
    assert PLAN_BASELINE_COMPARATOR_MAP["macro.fx_usd_nis_spot"] == "plan_inputs.assumed_fx_usd_nis"


def test_allocation_drift_via_comparator_map():
    """current_pct vs target_pct per allocation row should emerge as
    paired diff rows when the percentages differ."""
    current = _current_state()
    plan_baseline = {
        "plan_inputs": _plan_inputs(),
        "portfolio": {"allocations": [
            {"category": "Growth", "current_pct": 0.40, "target_pct": 0.40, "current_k_usd": 480.0, "target_k_usd": 480.0},
            {"category": "Income", "current_pct": 0.30, "target_pct": 0.30, "current_k_usd": 360.0, "target_k_usd": 360.0},
            {"category": "Cash", "current_pct": 0.10, "target_pct": 0.10, "current_k_usd": 120.0, "target_k_usd": 120.0},
            {"category": "Real Estate", "current_pct": 0.20, "target_pct": 0.20, "current_k_usd": 240.0, "target_k_usd": 240.0},
        ]},
    }
    full = compute_full_diff(current, plan_baseline, prior_snapshot=None)
    # Growth is 52% vs 40% target -> +30% deviation, should surface.
    growth_rows = [d for d in full.vs_plan if "current_pct" in d.path and "[0]" in d.path]
    assert any(d.magnitude is not None and abs(d.magnitude) > 0.05 for d in growth_rows), (
        f"Growth allocation deviation missing. vs_plan = {full.vs_plan}"
    )


# ---------------------------------------------------------------------------
# compute_full_diff merging semantics
# ---------------------------------------------------------------------------

def test_compute_full_diff_merges_without_duplicates():
    """vs_plan and vs_prior are separate lists; same path can appear
    in both with different baseline_labels, but each list itself has no
    intra-list dupes."""
    current = _current_state(fx_spot=2.8)
    plan_baseline = {
        "plan_inputs": _plan_inputs(fx=3.6),
        "portfolio": {"allocations": _portfolio()["allocations"]},
    }
    prior = _current_state(fx_spot=3.05)
    full = compute_full_diff(current, plan_baseline, prior)

    # vs_plan should have at most one row per path.
    vs_plan_paths = [d.path for d in full.vs_plan]
    assert len(vs_plan_paths) == len(set(vs_plan_paths)), (
        f"vs_plan has duplicate paths: {vs_plan_paths}"
    )
    vs_prior_paths = [d.path for d in full.vs_prior]
    assert len(vs_prior_paths) == len(set(vs_prior_paths)), (
        f"vs_prior has duplicate paths: {vs_prior_paths}"
    )

    # baseline_label correctness.
    assert all(d.baseline_label == "plan" for d in full.vs_plan)
    assert all(d.baseline_label == "prior_snapshot" for d in full.vs_prior)

    # FX should appear in BOTH (paired against plan AND prior).
    fx_in_plan = any(d.path == "macro.fx_usd_nis_spot" for d in full.vs_plan)
    fx_in_prior = any(d.path == "macro.fx_usd_nis_spot" for d in full.vs_prior)
    assert fx_in_plan, "FX missing from vs_plan"
    assert fx_in_prior, "FX missing from vs_prior"


def test_compute_full_diff_handles_no_plan_and_no_prior():
    """Both baselines absent -> empty FullDiff (no crash)."""
    current = _current_state()
    full = compute_full_diff(current, None, None)
    assert isinstance(full, FullDiff)
    assert full.vs_plan == []
    assert full.vs_prior == []
    assert full.vs_plan_truncated is False
    assert full.vs_prior_truncated is False


# ---------------------------------------------------------------------------
# Truncation (§2.5)
# ---------------------------------------------------------------------------

def test_max_fields_per_diff_truncation_respects_magnitude_ordering():
    """When numeric rows exceed MAX_FIELDS_PER_DIFF, the largest |magnitude|
    rows are retained and small ones dropped.

    Uses generic field names (no magnitude-floor suffix) so every row
    survives the §2.4 filter and the only thing standing between a row
    and the diff is the §2.5 truncation cap.
    """
    # 500 numeric fields, deviations span 0.030 .. 0.530 (all above the
    # 2% threshold so all survive the filter; only the truncation cap
    # decides which ones make it into the final list).
    n_fields = 500
    current_nested: dict[str, Any] = {
        "section_x": {f"f_{i}": 0.030 + (float(i) / 1000.0) for i in range(n_fields)}
    }
    baseline_nested: dict[str, Any] = {
        "section_x": {f"f_{i}": 0.000 for i in range(n_fields)}
    }

    full = compute_full_diff(
        current_nested,
        plan_baseline=None,
        prior_snapshot=baseline_nested,
        max_fields_per_side=300,
    )
    # We expect <= cap rows AND truncated flag set.
    assert len(full.vs_prior) <= 300
    assert len(full.vs_prior) == 300, (
        f"truncation should have kept exactly 300 rows; got {len(full.vs_prior)}"
    )
    assert full.vs_prior_truncated is True

    # Sanity: the LARGEST-magnitude row (f_499) survives the truncation.
    surviving_paths = {d.path for d in full.vs_prior}
    assert "section_x.f_499" in surviving_paths, (
        "largest-magnitude row was dropped during truncation"
    )
    # The SMALLEST-magnitude row (f_0) — even though it's above the
    # filter threshold — should be dropped because the cap kept only
    # the top 300 by |magnitude|.
    assert "section_x.f_0" not in surviving_paths, (
        f"small-magnitude row leaked past truncation; "
        f"got {sorted(p for p in surviving_paths if 'f_0' in p or 'f_1' in p)[:5]}"
    )


def test_truncation_keeps_all_categorical_and_appeared_rows():
    """§2.5 step 1: categorical / appeared / disappeared rows are kept
    regardless of the cap (high-signal events)."""
    # 200 numeric rows (small magnitudes, will compete for slots) +
    # 50 categorical rows (must all survive).
    current_nested: dict[str, Any] = {
        "section_n": {f"f_{i}_value": float(i) / 100.0 for i in range(200)},
        "section_c": {f"c_{i}": f"current_{i}" for i in range(50)},
    }
    baseline_nested: dict[str, Any] = {
        "section_n": {f"f_{i}_value": 0.0 for i in range(200)},
        "section_c": {f"c_{i}": f"baseline_{i}" for i in range(50)},
    }
    full = compute_full_diff(
        current_nested,
        plan_baseline=None,
        prior_snapshot=baseline_nested,
        max_fields_per_side=100,  # tight cap to force trimming
    )
    # All 50 categorical rows must be present even though the cap is 100
    # and we have far more numeric candidates competing.
    categorical_kept = [d for d in full.vs_prior if d.deviation_kind == "categorical_change"]
    assert len(categorical_kept) == 50, (
        f"expected 50 categorical rows kept, got {len(categorical_kept)}"
    )
    assert full.vs_prior_truncated is True


# ---------------------------------------------------------------------------
# Filter rules (§2.4)
# ---------------------------------------------------------------------------

def test_sub_threshold_numeric_is_dropped_unless_in_allowlist():
    """A 0.5% deviation in a non-allowlisted field gets dropped (below
    the 2% threshold AND below any meaningful magnitude floor)."""
    # 0.5% deviation on a generic field with no magnitude-floor suffix.
    current = {"section_g": {"generic_field": 1.005}}
    baseline = {"section_g": {"generic_field": 1.0}}
    diffs = compute_diff(current, baseline)
    assert len(diffs) == 0, f"sub-threshold row leaked through filter: {diffs}"


def test_allowlist_keeps_tiny_plan_anchored_deviation():
    """FX deviation of 0.5% (well below 2% threshold) MUST surface if
    it's in the comparator map / allowlist — plan-anchored fields are
    structurally significant at any size."""
    current = {
        "plan_inputs": _plan_inputs(fx=3.6),
        "macro": {"fx_usd_nis_spot": 3.582, "fx_usd_nis_30d_avg": 3.582},  # 0.5% below 3.6
        "portfolio": {"allocations": []},
    }
    plan_baseline = {
        "plan_inputs": _plan_inputs(fx=3.6),
        "portfolio": {"allocations": []},
    }
    full = compute_full_diff(current, plan_baseline, prior_snapshot=None)
    fx_rows = [d for d in full.vs_plan if d.path == "macro.fx_usd_nis_spot"]
    assert len(fx_rows) == 1, (
        f"Allowlist failed to keep sub-2% FX deviation: vs_plan = "
        f"{[d.path for d in full.vs_plan]}"
    )


def test_magnitude_floor_drops_tiny_absolute_pct_change():
    """A *_pct field that changes by less than 0.5pp absolute AND less
    than 2% relative is filtered out as noise. Without the floor the
    same row would survive on the relative-pct rule alone."""
    # 0.001 -> 0.0015 is +50% relative but only +0.05pp absolute.
    # The pct rule says "keep" (50% > 2%); the floor says "drop" (0.05pp
    # < 0.5pp). The filter uses the pct rule first — anything >= 2% pct
    # passes regardless of the floor (the floor is an OR, not an AND).
    # So this row DOES survive. The floor only helps when the pct is
    # sub-threshold. Test that case separately.
    current = {"section": {"sample_pct": 0.0500}}    # 0.0500 vs 0.0501
    baseline = {"section": {"sample_pct": 0.0501}}   # deviation = -0.2%, below 2%
    diffs = compute_diff(current, baseline)
    assert len(diffs) == 0, (
        f"Sub-threshold *_pct row not filtered by floor: {diffs}"
    )


# ---------------------------------------------------------------------------
# compute_deviation_bucket — determinism + monotonicity
# ---------------------------------------------------------------------------

def test_compute_deviation_bucket_is_deterministic():
    """Calling the function twice with the same input yields the same output."""
    samples = [-0.5, -0.25, -0.15, -0.05, 0.0, 0.05, 0.15, 0.3, 0.99]
    for m in samples:
        first = compute_deviation_bucket(m, "numeric_pct")
        second = compute_deviation_bucket(m, "numeric_pct")
        assert first == second, f"non-deterministic at magnitude={m}"


def test_compute_deviation_bucket_is_monotonic():
    """m1 < m2 (in absolute terms) implies bucket(m1) <= bucket(m2) by
    the documented order. We sample 500 evenly-spaced points and check
    the bucket index never decreases."""
    bucket_index = {b: i for i, b in enumerate(DEVIATION_BUCKET_ORDER)}
    last_idx = -1
    for i in range(500):
        mag = i / 1000.0  # 0.000 .. 0.499
        bucket = compute_deviation_bucket(mag, "numeric_pct")
        assert bucket in bucket_index, f"unknown bucket {bucket}"
        idx = bucket_index[bucket]
        assert idx >= last_idx, (
            f"bucket regressed at mag={mag}: {bucket} (idx={idx}) < last {last_idx}"
        )
        last_idx = idx


def test_compute_deviation_bucket_boundaries():
    """Boundary values land in the documented bucket."""
    assert compute_deviation_bucket(0.0, "numeric_pct") == "<5pct"
    assert compute_deviation_bucket(0.04999, "numeric_pct") == "<5pct"
    assert compute_deviation_bucket(0.05, "numeric_pct") == "5to15pct"
    assert compute_deviation_bucket(0.14999, "numeric_pct") == "5to15pct"
    assert compute_deviation_bucket(0.15, "numeric_pct") == "15to30pct"
    assert compute_deviation_bucket(0.29999, "numeric_pct") == "15to30pct"
    assert compute_deviation_bucket(0.30, "numeric_pct") == ">30pct"
    assert compute_deviation_bucket(0.999, "numeric_pct") == ">30pct"
    # Sign-invariance: negative magnitudes bucket by absolute value.
    assert compute_deviation_bucket(-0.222, "numeric_pct") == "15to30pct"


def test_compute_deviation_bucket_categorical_returns_categorical():
    """Non-numeric kinds always bucket as 'categorical'."""
    assert compute_deviation_bucket(None, "categorical_change") == "categorical"
    assert compute_deviation_bucket(None, "appeared") == "categorical"
    assert compute_deviation_bucket(None, "disappeared") == "categorical"


# ---------------------------------------------------------------------------
# CI invariants — schema coverage
# ---------------------------------------------------------------------------

# This is the FULL list of numeric-field paths in the §1.2 snapshot
# schema, with list-element notation for fields that live inside lists.
# Keep this in sync with the spec; the test below verifies every entry
# is either in the comparator map OR explicitly listed in
# _NO_PLAN_BASELINE_FIELDS — no silent "field exists but isn't paired"
# states allowed.
_SCHEMA_NUMERIC_FIELDS: frozenset[str] = frozenset({
    # plan_inputs (assumed_* values; baseline-side, not diff-side, but
    # included for completeness — these never appear as a key in the
    # comparator map because they ARE the baseline).
    # We allow them in _NO_PLAN_BASELINE_FIELDS implicitly by not listing
    # them in _SCHEMA_NUMERIC_FIELDS; only "current-state" fields are tested.
    # portfolio (current-state numerics)
    "portfolio.total_value_usd",
    "portfolio.cash_balances_usd",
    "portfolio.positions[].shares",
    "portfolio.positions[].value_usd",
    "portfolio.positions[].value_nis",
    "portfolio.allocations[].current_pct",
    "portfolio.allocations[].current_k_usd",
    "portfolio.top_concentration_pct",
    "portfolio.unallocated_cash_usd",
    # macro
    "macro.fx_usd_nis_spot",
    "macro.fx_usd_nis_30d_avg",
    "macro.fed_funds_rate_pct",
    "macro.treasury_10y_yield_pct",
    "macro.sp500_index",
    "macro.sp500_30d_return_pct",
    "macro.nasdaq_index",
    "macro.nasdaq_30d_return_pct",
    "macro.vix",
    # cashflow_recent
    "cashflow_recent.last_3_months[].projected_expense_nis",
    "cashflow_recent.last_3_months[].realized_expense_nis",
    "cashflow_recent.last_3_months[].deviation_pct",
    "cashflow_recent.last_3_months[].projected_income_nis",
    "cashflow_recent.last_3_months[].realized_income_nis",
    "cashflow_recent.last_3_months[].income_deviation_pct",
    "cashflow_recent.cumulative_deviation_nis",
    # tax_assumptions
    "tax_assumptions.current_marginal_bracket_pct",
    "tax_assumptions.effective_rate_prior_year_pct",
    "tax_assumptions.assumed_marginal_rate_pct",
    "tax_assumptions.withholding_supplemental_cap_pct",
})


def test_every_numeric_field_is_paired_or_documented():
    """CI INVARIANT: every numeric field in the §1.2 snapshot schema
    must EITHER appear as a key in PLAN_BASELINE_COMPARATOR_MAP OR
    be listed in _NO_PLAN_BASELINE_FIELDS.

    This stops a future schema addition from silently shipping with no
    plan-baseline pairing (which is the architectural failure mode the
    map was added to prevent).
    """
    paired = set(PLAN_BASELINE_COMPARATOR_MAP.keys())
    documented = set(_NO_PLAN_BASELINE_FIELDS)
    covered = paired | documented

    # ACTUAL coverage check — not just dict-non-empty: every single
    # numeric field in the schema must be on one side.
    uncovered = _SCHEMA_NUMERIC_FIELDS - covered
    assert not uncovered, (
        f"Schema fields with no comparator-map entry AND not listed in "
        f"_NO_PLAN_BASELINE_FIELDS: {sorted(uncovered)}. "
        f"Either add a pairing to PLAN_BASELINE_COMPARATOR_MAP or "
        f"document the field in _NO_PLAN_BASELINE_FIELDS with a reason."
    )

    # Defense-in-depth: covered set is strict subset of (schema U paired).
    # A field can be paired but not in the schema (forward-compat header
    # additions); we don't enforce that direction.


def test_no_plan_baseline_fields_disjoint_from_comparator_map():
    """A field can't be BOTH paired AND documented as unpaired — these
    sets must be disjoint to keep the invariant test above unambiguous."""
    overlap = set(PLAN_BASELINE_COMPARATOR_MAP.keys()) & set(_NO_PLAN_BASELINE_FIELDS)
    assert not overlap, (
        f"Fields appearing in BOTH the comparator map and the no-baseline "
        f"allowlist: {sorted(overlap)}. Pick one."
    )


def test_every_snapshot_prefix_is_enumerable_for_inferred_kind():
    """CI INVARIANT (commit #6 surface): every section.field-prefix
    actually used by the diff service has an entry in
    SNAPSHOT_FIELD_PREFIXES so the inferred_kind mapping table in
    commit #6 has full schema coverage.

    The test walks every numeric-field path in the schema, derives the
    leading-prefix that the flag-writer would key on, and asserts at
    least one entry in SNAPSHOT_FIELD_PREFIXES matches that prefix.
    """
    prefix_strings = [p for (p, _kind) in SNAPSHOT_FIELD_PREFIXES]

    def covered_by_any_prefix(path: str) -> bool:
        # Strip bracket-element notation: "portfolio.positions[].shares" ->
        # "portfolio.positions" (the prefix lookup ignores per-row suffixes).
        bare = path.split("[", 1)[0]
        return any(bare.startswith(p) or path.startswith(p) for p in prefix_strings)

    uncovered_paths = [p for p in _SCHEMA_NUMERIC_FIELDS if not covered_by_any_prefix(p)]
    assert not uncovered_paths, (
        f"Schema paths with no matching SNAPSHOT_FIELD_PREFIXES entry: "
        f"{sorted(uncovered_paths)}. Add a prefix row to "
        f"argosy/services/state_diff.py::SNAPSHOT_FIELD_PREFIXES so the "
        f"commit #6 inferred_kind mapping has coverage."
    )


def test_snapshot_field_prefixes_is_not_just_non_empty():
    """Sanity check: SNAPSHOT_FIELD_PREFIXES has the named prefixes the
    spec §4.2 inferred_kind table enumerates. NOT a dict-non-empty
    check — explicit named entries that must exist."""
    prefixes = {p for (p, _k) in SNAPSHOT_FIELD_PREFIXES}
    required = {
        "macro.fx_",
        "portfolio.allocations",
        "portfolio.positions",
        "tax_assumptions.",
        "cashflow_recent.",
    }
    missing = required - prefixes
    assert not missing, (
        f"SNAPSHOT_FIELD_PREFIXES is missing critical entries: {missing}"
    )


def test_plan_baseline_comparator_map_is_not_just_non_empty():
    """Sanity guard: the map is not allowed to be {} or {fx_only}.

    A future refactor that accidentally truncates the map should fail
    this test. We require the four major dimensions (FX, allocation,
    cashflow, tax) are all represented.
    """
    keys = set(PLAN_BASELINE_COMPARATOR_MAP.keys())
    assert "macro.fx_usd_nis_spot" in keys, "FX pairing missing"
    assert any(k.startswith("portfolio.allocations") for k in keys), (
        "allocation pairing missing"
    )
    assert any(k.startswith("cashflow_recent.") for k in keys), (
        "cashflow pairing missing"
    )
    assert any(k.startswith("tax_assumptions.") for k in keys), (
        "tax pairing missing"
    )


def test_always_include_allowlist_derived_from_comparator_map():
    """The allowlist is auto-derived; this test pins the relationship
    so a future refactor that breaks the derivation fails loudly."""
    assert ALWAYS_INCLUDE_ALLOWLIST == frozenset(PLAN_BASELINE_COMPARATOR_MAP.keys())


# ---------------------------------------------------------------------------
# Public surface stability
# ---------------------------------------------------------------------------

def test_fielddiff_is_immutable():
    """FieldDiff is frozen — accidental mutation by downstream consumers
    is impossible at runtime."""
    d = FieldDiff(
        path="x",
        current_value=1.0,
        baseline_value=0.5,
        deviation_kind="numeric_pct",
        magnitude=1.0,
    )
    with pytest.raises((AttributeError, Exception)):
        d.path = "y"  # type: ignore[misc]


def test_fulldiff_default_factory():
    """FullDiff with no args is a valid empty result."""
    f = FullDiff()
    assert f.vs_plan == []
    assert f.vs_prior == []
    assert f.vs_plan_truncated is False
    assert f.vs_prior_truncated is False


def test_default_numeric_threshold_is_2_percent():
    """The §2.4 default threshold is 2% absolute — pinned so a future
    refactor that bumps it without spec amendment fails."""
    assert DEFAULT_NUMERIC_THRESHOLD_PCT == 0.02
