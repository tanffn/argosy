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
