"""The deterministic plan Risk/Constraint Kernel — re-derives portfolio physics from
RAW holdings and BLOCKs on a violation, blind to any fleet/LLM reasoning. This is both
the refinement money-safety gate and Argosy's anti-correlation review gate
(see docs/superpowers/specs/2026-07-03-incremental-plan-refinement.md and
[[feedback_adversarial_review_must_re_derive_blind]]).

Slice 1: the single-name cap measured on a DIRECT + FUND-LOOK-THROUGH basis — the exact
constraint the plan fleet missed (a 12% direct NVDA target can still breach a 13% total
cap once CSPX/R1GR embedded NVDA is counted).
"""
from __future__ import annotations

from types import SimpleNamespace

from argosy.quality.plan_risk_kernel import (
    evaluate_plan_target_single_name_cap,
    evaluate_single_name_cap,
    evaluate_allocation_sum,
    evaluate_us_situs,
    evaluate_plan_invariants,
)


def _fake_effective(weights: dict[str, float]):
    """A deterministic look-through stand-in: direct NVDA counts fully; every other
    symbol contributes `weights[sym]` of its value as embedded single-name exposure."""
    def fn(sym: str, val: float) -> float:
        if sym.upper() == "NVDA":
            return val
        return val * weights.get(sym.upper(), 0.0)
    return fn


def test_flags_lookthrough_breach_even_when_direct_is_under_cap():
    # Direct NVDA is 10% of the book — UNDER a 13% cap. But CSPX (900k) embeds 7% NVDA,
    # so the LOOK-THROUGH single-name exposure is 16.3% > 13%. A direct-only check misses
    # this; the kernel must catch it.
    holdings = {"NVDA": 100_000.0, "CSPX": 900_000.0}
    r = evaluate_single_name_cap(
        holdings_usd=holdings, cap_pct=13.0, effective_fn=_fake_effective({"CSPX": 0.07}),
    )
    assert not r.ok
    assert any(v.code == "single_name_lookthrough_cap" for v in r.violations)
    assert round(r.single_name_lookthrough_pct, 1) == 16.3


def test_ok_when_under_cap():
    holdings = {"NVDA": 50_000.0, "EXUS": 950_000.0}  # EXUS 0% NVDA
    r = evaluate_single_name_cap(
        holdings_usd=holdings, cap_pct=13.0, effective_fn=_fake_effective({}),
    )
    assert r.ok
    assert round(r.single_name_lookthrough_pct, 1) == 5.0


def test_proposed_buys_are_counted_post_trade():
    # Currently fine (only 100k direct NVDA in a 100k book would be 100%, so add ex-US),
    # but a PROPOSED buy of a NVDA-heavy fund pushes look-through over the cap.
    holdings = {"NVDA": 100_000.0, "EXUS": 800_000.0}
    r = evaluate_single_name_cap(
        holdings_usd=holdings,
        proposed_buys={"CSPX": 900_000.0},              # 7% NVDA embedded
        cap_pct=13.0,
        effective_fn=_fake_effective({"CSPX": 0.07}),
    )
    # post book = 1,800,000; single-name = 100,000 + 63,000 = 163,000 = 9.06% — under.
    assert r.ok
    # Now a bigger NVDA-heavy buy tips it over:
    r2 = evaluate_single_name_cap(
        holdings_usd={"NVDA": 200_000.0},
        proposed_buys={"CSPX": 800_000.0},
        cap_pct=13.0,
        effective_fn=_fake_effective({"CSPX": 0.07}),
    )
    # book = 1,000,000; single-name = 200,000 + 56,000 = 256,000 = 25.6% > 13.
    assert not r2.ok
    assert round(r2.single_name_lookthrough_pct, 1) == 25.6


def _doc(classes, cap=13.0):
    return SimpleNamespace(
        nvda_cap_pct=cap,
        classes=[
            SimpleNamespace(
                target_pct=tp,
                instruments=[
                    SimpleNamespace(symbol=s, weight_within_class_pct=w) for s, w in inst
                ],
            )
            for tp, inst in classes
        ],
    )


def test_plan_target_cap_flags_embedded_breach():
    # 12% direct NVDA sleeve is UNDER a 13% cap, but the 88% US sleeve embeds 10% NVDA,
    # so the TARGET end-state is 20.8% single-name on look-through — the plan breaches its
    # own cap. This is the fleet miss the gate must catch.
    doc = _doc([(12.0, [("NVDA", 100.0)]), (88.0, [("CSPX", 100.0)])])
    r = evaluate_plan_target_single_name_cap(doc, effective_fn=_fake_effective({"CSPX": 0.10}))
    assert not r.ok
    assert round(r.single_name_lookthrough_pct, 1) == 20.8


def test_plan_target_cap_ok_when_within():
    doc = _doc([(10.0, [("NVDA", 100.0)]), (90.0, [("EXUS", 100.0)])])  # EXUS 0% NVDA
    r = evaluate_plan_target_single_name_cap(doc, effective_fn=_fake_effective({}))
    assert r.ok
    assert round(r.single_name_lookthrough_pct, 1) == 10.0


# ---------------------------------------------------------------------------
# Slice 2 tests: evaluate_allocation_sum
# ---------------------------------------------------------------------------


def test_allocation_sum_within_tolerance():
    # 40 + 60 = 100.0 — no violation.
    doc = _doc([(40.0, [("EXUS", 100.0)]), (60.0, [("NVDA", 100.0)])])
    r = evaluate_allocation_sum(doc)
    assert r.ok
    assert abs(r.total_pct - 100.0) < 0.01
    assert r.violations == ()


def test_allocation_sum_over_tolerance_flags_violation():
    # 40 + 61 = 101.0 — exceeds 0.5pp default tolerance → violation.
    doc = _doc([(40.0, [("EXUS", 100.0)]), (61.0, [("NVDA", 100.0)])])
    r = evaluate_allocation_sum(doc)
    assert not r.ok
    assert any(v.code == "allocation_sum" for v in r.violations)
    assert "101" in r.violations[0].detail or "101.0" in r.violations[0].detail


def test_allocation_sum_under_tolerance_does_not_flag():
    # 40 + 60.3 = 100.3 — within default 0.5pp tolerance.
    doc = _doc([(40.0, [("EXUS", 100.0)]), (60.3, [("NVDA", 100.0)])])
    r = evaluate_allocation_sum(doc)
    assert r.ok


def test_allocation_sum_custom_tolerance():
    # 40 + 60.4 = 100.4 — within 0.5 but over a strict 0.1pp tolerance.
    doc = _doc([(40.0, [("EXUS", 100.0)]), (60.4, [("NVDA", 100.0)])])
    r = evaluate_allocation_sum(doc, tolerance_pp=0.1)
    assert not r.ok
    assert any(v.code == "allocation_sum" for v in r.violations)


def test_allocation_sum_zero_classes():
    # Empty doc → 0%, which deviates from 100 by 100pp → violation.
    doc = SimpleNamespace(nvda_cap_pct=13.0, classes=[])
    r = evaluate_allocation_sum(doc)
    assert not r.ok
    assert any(v.code == "allocation_sum" for v in r.violations)


# ---------------------------------------------------------------------------
# Slice 2 tests: evaluate_us_situs
# ---------------------------------------------------------------------------


def _us_situs_estate_safe_fn(safe_syms: set[str]):
    """Returns an estate_safe_fn: sym -> bool. True = estate-safe (UCITS/non-US-situs)."""
    def fn(sym: str) -> bool:
        return sym.upper() in safe_syms
    return fn


def test_us_situs_flags_unsanctioned_us_domiciled_buy():
    # SPY is US-situs, not in sanctioned set (default only NVDA) → violation.
    r = evaluate_us_situs(
        holdings_usd={"EXUS": 100_000.0},
        proposed_buys={"SPY": 10_000.0},
        estate_safe_fn=_us_situs_estate_safe_fn(set()),   # SPY is NOT estate-safe
    )
    assert not r.ok
    assert any(v.code == "us_situs" for v in r.violations)
    assert "SPY" in r.violations[0].detail


def test_us_situs_does_not_flag_sanctioned_nvda():
    # NVDA is explicitly sanctioned → no violation even though US-situs.
    r = evaluate_us_situs(
        holdings_usd={"EXUS": 100_000.0},
        proposed_buys={"NVDA": 5_000.0},
        estate_safe_fn=_us_situs_estate_safe_fn(set()),   # NVDA not estate-safe but sanctioned
    )
    assert r.ok
    assert r.violations == ()


def test_us_situs_does_not_flag_estate_safe_ucits():
    # CSPX is UCITS (Irish-domiciled) → estate_safe → no violation.
    r = evaluate_us_situs(
        holdings_usd={},
        proposed_buys={"CSPX": 50_000.0},
        estate_safe_fn=_us_situs_estate_safe_fn({"CSPX"}),  # CSPX is estate-safe
    )
    assert r.ok


def test_us_situs_does_not_flag_existing_holdings():
    # Existing US-situs holdings are NOT flagged — only NEW proposed buys matter.
    r = evaluate_us_situs(
        holdings_usd={"SPY": 200_000.0},   # already held — not our concern today
        proposed_buys={},
        estate_safe_fn=_us_situs_estate_safe_fn(set()),
    )
    assert r.ok


def test_us_situs_multiple_buys_flags_only_unsafe():
    # Propose: SPY (unsafe, unsanctioned), CSPX (estate-safe), NVDA (sanctioned).
    r = evaluate_us_situs(
        holdings_usd={},
        proposed_buys={"SPY": 5_000.0, "CSPX": 5_000.0, "NVDA": 5_000.0},
        estate_safe_fn=_us_situs_estate_safe_fn({"CSPX"}),
    )
    assert not r.ok
    assert sum(1 for v in r.violations if v.code == "us_situs") == 1
    assert any("SPY" in v.detail for v in r.violations)


# ---------------------------------------------------------------------------
# Slice 2 tests: evaluate_plan_invariants (aggregator)
# ---------------------------------------------------------------------------


def test_invariants_all_ok():
    # Clean doc: 90+10=100%, NVDA is only 10% (well under 13% cap), no us_situs buy.
    doc = _doc([(90.0, [("EXUS", 100.0)]), (10.0, [("NVDA", 100.0)])])
    report = evaluate_plan_invariants(
        doc,
        holdings_usd={"EXUS": 90_000.0, "NVDA": 10_000.0},
        proposed_buys={},
        effective_fn=_fake_effective({}),
        estate_safe_fn=_us_situs_estate_safe_fn(set()),
    )
    assert report.ok
    assert report.violations == ()
    assert "single_name_cap" in report.parts
    assert "allocation_sum" in report.parts
    assert "us_situs" in report.parts


def test_invariants_aggregates_multiple_violations():
    # Doc has 101% sum AND proposes a US-situs buy → two distinct violations.
    doc = _doc([(41.0, [("EXUS", 100.0)]), (60.0, [("NVDA", 100.0)])])  # 101% sum
    report = evaluate_plan_invariants(
        doc,
        holdings_usd={"EXUS": 41_000.0, "NVDA": 60_000.0},
        proposed_buys={"SPY": 5_000.0},   # US-situs, unsanctioned
        effective_fn=_fake_effective({}),
        estate_safe_fn=_us_situs_estate_safe_fn(set()),
    )
    assert not report.ok
    codes = {v.code for v in report.violations}
    assert "allocation_sum" in codes
    assert "us_situs" in codes


def test_invariants_without_holdings_skips_us_situs():
    # No holdings/proposed_buys → us_situs check is skipped, still aggregates others.
    # Use a doc where NVDA is under the 13% cap so the only check that could fail is skipped.
    doc = _doc([(90.0, [("EXUS", 100.0)]), (10.0, [("NVDA", 100.0)])])
    report = evaluate_plan_invariants(
        doc,
        effective_fn=_fake_effective({}),
        estate_safe_fn=_us_situs_estate_safe_fn(set()),
    )
    assert report.ok
    assert "us_situs" not in report.parts  # not run — no holdings context
