"""Deterministic corrections-landed floor — docs/design/corrective_resynthesis.md §2.C.1.

Gate outcomes per the design test plan: landed / not-landed /
cosmetically-absorbed (canonical value present but the wrong value survives)
→ proceed / fail / fail.
"""

from __future__ import annotations

from argosy.quality.corrections_check import (
    check_corrections_landed,
    value_variants,
)


def _corr(index=1, topic="nvda-target", canonical=None, wrong=None):
    return {
        "index": index, "topic": topic, "plan_item_ref": "targets.nvda",
        "canonical_values": canonical or [], "wrong_values": wrong or [],
    }


def test_landed_when_canonical_present_and_wrong_absent():
    res = check_corrections_landed(
        corrections=[_corr(canonical=[4136], wrong=[2201])],
        surfaces={"long": "Retain ~4,136 NVDA shares at the 8% target."},
    )
    assert res.passes
    assert res.unresolved_payload() == []
    assert "all landed" in res.summary()


def test_not_landed_when_canonical_absent():
    res = check_corrections_landed(
        corrections=[_corr(canonical=[4136])],
        surfaces={"long": "Retain roughly three thousand shares."},
    )
    assert not res.passes
    unresolved = res.unresolved_payload()
    assert len(unresolved) == 1
    assert "absent" in unresolved[0]["reason"]


def test_cosmetically_absorbed_fails_wrong_value_still_present():
    """Canonical value pasted in while another surface still asserts the
    contradicted figure — the surface contradicts, so NOT landed."""
    res = check_corrections_landed(
        corrections=[_corr(canonical=[4136], wrong=[2201])],
        surfaces={
            "long": "Retain ~4,136 NVDA shares.",
            "medium": "Target of 2,201 shares stands for 2026.",
        },
    )
    assert not res.passes
    assert "wrong value" in res.unresolved_payload()[0]["reason"]


def test_numeric_boundary_no_substring_false_positive():
    res = check_corrections_landed(
        corrections=[_corr(canonical=[4136])],
        surfaces={"long": "position id 14136 and lot 41365 are unrelated"},
    )
    assert not res.passes  # 4136 inside 14136/41365 must NOT count


def test_comma_grouped_variant_matches():
    assert "4,136" in value_variants(4136)
    res = check_corrections_landed(
        corrections=[_corr(canonical=[4136])],
        surfaces={"alloc_json": '{"nvda_target_sh": 4136}'},
    )
    assert res.passes


def test_float_variants():
    assert "2.944" in value_variants(2.944)
    res = check_corrections_landed(
        corrections=[_corr(canonical=[2.944], wrong=[3.00])],
        surfaces={"long": "planning FX of 2.944 USD/NIS"},
    )
    assert res.passes


def test_wrong_float_still_present_fails():
    res = check_corrections_landed(
        corrections=[_corr(canonical=[2.944], wrong=[3.0])],
        surfaces={"long": "planning FX of 2.944; dashboard shows 3 NIS"},
    )
    assert not res.passes


def test_no_deterministic_values_passes_floor():
    """Substance-only corrections belong to the reader's judgment pass —
    the deterministic floor never blocks on them."""
    res = check_corrections_landed(
        corrections=[_corr(canonical=[], wrong=[])],
        surfaces={"long": "anything"},
    )
    assert res.passes
    assert "judgment pass" in res.checks[0].reason


def test_all_canonical_values_required():
    """Codex finding #5: 'any of' let one leg of a multi-value correction
    (e.g. a three-year glide schedule) pass — EVERY canonical value must land."""
    res = check_corrections_landed(
        corrections=[_corr(canonical=[4136, 5094, 592])],
        surfaces={"long": "2026: sell 4,136 sh; later years TBD"},
    )
    assert not res.passes
    reason = res.unresolved_payload()[0]["reason"]
    assert "5,094" in reason and "592" in reason and "4,136" not in reason
    res2 = check_corrections_landed(
        corrections=[_corr(canonical=[4136, 5094, 592])],
        surfaces={"long": "glide: 4,136 / 5,094 / 592 sh"},
    )
    assert res2.passes


def test_numeric_boundary_decimal_continuation():
    """Codex finding #10: '4136' must not match inside '4136.5', while
    sentence punctuation after the number still counts."""
    res = check_corrections_landed(
        corrections=[_corr(canonical=[4136])],
        surfaces={"long": "an unrelated 4136.5 figure"},
    )
    assert not res.passes
    res2 = check_corrections_landed(
        corrections=[_corr(canonical=[4136])],
        surfaces={"long": "retain 4,136, then reassess. Final: 4,136."},
    )
    assert res2.passes


def test_run_156_numeric_string_canonical_matches_pct_rendering():
    """Run-156 regression (2026-07-09): the zigzag settlement produced the
    canonical as STRING '13.0'; the draft writes 'cap of 13%'. The variant
    set must bridge trailing-zero float ↔ int symmetrically — '13.0' was
    absent, the skeleton gate failed after retry, and the run degraded to
    the monolith."""
    variants = value_variants("13.0")
    assert "13" in variants and "13.0" in variants
    res = check_corrections_landed(
        corrections=[_corr(canonical=["13.0"])],
        surfaces={"long": "NVDA concentration: hard cap of 13% single-name."},
    )
    assert res.passes, res.summary()


def test_numeric_string_variants_symmetric():
    # string ↔ JSON numeric, with/without '%', trailing-zero float ↔ int.
    assert "13.0" in value_variants("13")
    assert "13.0" in value_variants(13)
    assert "13" in value_variants(13.0)
    assert "13" in value_variants("13.0%")
    assert "4,136" in value_variants("4136.0")
    # Non-numeric strings stay exact-match only.
    assert value_variants("fast-on-eligible-core") == ["fast-on-eligible-core"]


def test_numeric_string_canonical_keeps_digit_boundary_guards():
    """Widening must NOT loosen the boundary guards: canonical '13.0' still
    must not match inside 130, 4.13, or 13.5."""
    res = check_corrections_landed(
        corrections=[_corr(canonical=["13.0"])],
        surfaces={"long": "lot 130 sold at 4.13 with a 13.5 multiple"},
    )
    assert not res.passes


def test_string_canonical_value():
    res = check_corrections_landed(
        corrections=[_corr(canonical=["fast-on-eligible-core"])],
        surfaces={"long": "glide policy: fast-on-eligible-core (§102-feasible)"},
    )
    assert res.passes
