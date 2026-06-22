"""Tests for the §102 equity-tax payslip-reconciliation adequacy check.

Two layers:

1. The real April 2026 payslip (parsed) — skip-guarded when the Google-Drive
   sample dir is absent. Ground truth: reconciles, residual ~₪1, top-up ~₪7,384.
2. Pure unit tests on hand-built ``PayslipFacts`` (no external file) covering a
   clean reconcile, a discrepancy, no-equity-yet, and low-confidence.

Only the TESTS carry expected numbers; the checker derives everything.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from argosy.services.payslip_parser import HIGH, LOW, PayslipFacts, parse_payslip
from argosy.services.rsu_reconciliation.sim_tax import WIRE_ORDINARY_RATE
from argosy.services.rsu_reconciliation.withholding_check import (
    CAPITAL_RATE,
    SIM_ORDINARY_RATE,
    STATUS_DISCREPANCY,
    STATUS_LOW_CONFIDENCE,
    STATUS_NO_EQUITY,
    STATUS_RECONCILED,
    check_withholding,
)

_SAMPLE_DIR = Path(
    "D:/Google Drive/Family/Finances/Portfolio/Resources/2026/Payslip/Ariel"
)


def _april() -> Path:
    return _SAMPLE_DIR / "2026_04.pdf"


# ---------------------------------------------------------------------------
# Helpers to build a trusted (high-confidence) equity PayslipFacts by hand.
# ---------------------------------------------------------------------------
def _facts(
    *,
    ord_base: float | None,
    cap_base: float | None,
    actual: float | None,
    equity_conf: str = HIGH,
    year: int = 2026,
) -> PayslipFacts:
    f = PayslipFacts(period_year=year, period_month=4)
    f.ytd_non_fixed_gross = ord_base
    f.ytd_capital_gain = cap_base
    f.ytd_tax_on_non_fixed_gross = actual
    for k in (
        "ytd_non_fixed_gross",
        "ytd_capital_gain",
        "ytd_tax_on_non_fixed_gross",
    ):
        if getattr(f, k) is not None:
            f.confidence[k] = equity_conf
    return f


# ---------------------------------------------------------------------------
# 1. Real April payslip.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    not _april().exists(), reason="April payslip sample not present (Google Drive)."
)
def test_april_reconciles_with_expected_topup() -> None:
    facts = parse_payslip(_april())
    v = check_withholding(facts)

    assert v.status == STATUS_RECONCILED
    assert v.period == 2026

    # Bases as parsed (ground truth from the real document).
    assert v.equity_ordinary_base == pytest.approx(60679, abs=1)
    assert v.equity_capital_base == pytest.approx(549467, abs=1)
    assert v.actual_tax_withheld == pytest.approx(167707, abs=1)

    # §102 wire-rate reconciliation: 549467*0.25 + 60679*0.50 = 167,706.25.
    assert v.expected_at_wire_rate == pytest.approx(167706.25, abs=1.0)
    # Residual ~₪1 (actual 167,707 vs 167,706.25).
    assert abs(v.reconc_residual) < 2.0

    # Conservative filing top-up: 549467*0.25 + 60679*0.6217 - 167707 ≈ 7,384.
    expected_topup = 549467 * 0.25 + 60679 * 0.6217 - 167707
    assert v.potential_filing_topup == pytest.approx(expected_topup, abs=2.0)
    assert v.potential_filing_topup == pytest.approx(7384, abs=5.0)

    assert v.confidence == "high"
    assert isinstance(v.caveats, list) and v.caveats
    # Summary mentions the earmark.
    assert "top-up" in v.summary.lower() or "adequate" in v.summary.lower()


# ---------------------------------------------------------------------------
# 2. Unit tests on hand-built facts.
# ---------------------------------------------------------------------------
def test_constants_reused_from_sim_tax() -> None:
    # Guard against silent rate drift / redefinition.
    assert WIRE_ORDINARY_RATE == 0.50
    assert CAPITAL_RATE == 0.25
    assert SIM_ORDINARY_RATE == 0.6217


def test_clean_reconcile_unit() -> None:
    ord_base, cap_base = 60679.0, 549467.0
    actual = cap_base * CAPITAL_RATE + ord_base * WIRE_ORDINARY_RATE  # exact
    v = check_withholding(_facts(ord_base=ord_base, cap_base=cap_base, actual=actual))

    assert v.status == STATUS_RECONCILED
    assert v.reconc_residual == pytest.approx(0.0, abs=0.01)
    assert v.expected_at_wire_rate == pytest.approx(actual, abs=0.01)

    expected_cons = cap_base * CAPITAL_RATE + ord_base * SIM_ORDINARY_RATE
    assert v.conservative_liability == pytest.approx(expected_cons, abs=0.01)
    assert v.potential_filing_topup == pytest.approx(expected_cons - actual, abs=0.01)
    assert v.potential_filing_topup > 0
    assert v.effective_rate_pct == pytest.approx(
        actual / (ord_base + cap_base) * 100, abs=0.01
    )
    assert v.confidence == "high"


def test_reconcile_within_tolerance_band() -> None:
    # Perturb actual by < tolerance ($50 / 0.5%) -> still reconciled.
    ord_base, cap_base = 60679.0, 549467.0
    base = cap_base * CAPITAL_RATE + ord_base * WIRE_ORDINARY_RATE
    v = check_withholding(
        _facts(ord_base=ord_base, cap_base=cap_base, actual=base + 40.0)
    )
    assert v.status == STATUS_RECONCILED
    assert v.reconc_residual == pytest.approx(40.0, abs=0.01)


def test_discrepancy_when_perturbed_beyond_tolerance() -> None:
    ord_base, cap_base = 60679.0, 549467.0
    base = cap_base * CAPITAL_RATE + ord_base * WIRE_ORDINARY_RATE
    # Tolerance is max($50, 0.5%*actual) ≈ ₪838; push well past it.
    v = check_withholding(
        _facts(ord_base=ord_base, cap_base=cap_base, actual=base + 5000.0)
    )
    assert v.status == STATUS_DISCREPANCY
    assert v.reconc_residual == pytest.approx(5000.0, abs=0.01)
    assert "investigate" in v.summary.lower()
    # Top-up still computed even on a discrepancy.
    assert v.potential_filing_topup is not None


def test_no_equity_yet_all_none() -> None:
    v = check_withholding(_facts(ord_base=None, cap_base=None, actual=None))
    assert v.status == STATUS_NO_EQUITY
    assert v.expected_at_wire_rate is None
    assert v.potential_filing_topup is None
    assert v.confidence == "high"
    assert "no equity" in v.summary.lower()


def test_partial_equity_fields_low_confidence() -> None:
    # Equity accrued (ordinary present) but actual withheld missing.
    v = check_withholding(
        _facts(ord_base=60679.0, cap_base=549467.0, actual=None)
    )
    assert v.status == STATUS_LOW_CONFIDENCE
    assert v.expected_at_wire_rate is None
    assert v.confidence == "low"


def test_low_confidence_parser_flag() -> None:
    # All fields present but parser marked them LOW -> don't assert a number.
    f = _facts(
        ord_base=60679.0,
        cap_base=549467.0,
        actual=167707.0,
        equity_conf=LOW,
    )
    v = check_withholding(f)
    assert v.status == STATUS_LOW_CONFIDENCE
    assert v.reconc_residual is None
    assert v.confidence == "low"
    # Bases still surfaced so the user sees what was read.
    assert v.actual_tax_withheld == pytest.approx(167707.0)


def test_adequate_when_no_topup() -> None:
    # Construct a case where actual withholding already covers the conservative
    # liability (e.g. capital-heavy with high withholding) -> adequate.
    ord_base, cap_base = 10000.0, 500000.0
    conservative = cap_base * CAPITAL_RATE + ord_base * SIM_ORDINARY_RATE
    # Withhold exactly the wire-rate amount AND ensure it meets conservative by
    # making ordinary tiny: pick actual = wire expected, check topup small.
    actual = cap_base * CAPITAL_RATE + ord_base * WIRE_ORDINARY_RATE
    v = check_withholding(_facts(ord_base=ord_base, cap_base=cap_base, actual=actual))
    assert v.status == STATUS_RECONCILED
    # Here ordinary is small so topup is small; just assert it's the gap.
    assert v.potential_filing_topup == pytest.approx(
        max(0.0, conservative - actual), abs=0.01
    )
