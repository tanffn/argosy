"""Tests for the Hilan payslip parser against the REAL 2026 sample payslips.

The samples live on a Google Drive path that is not present in CI; every
test skips gracefully when the directory is absent so CI without the files
does not fail. All expected values below are the human-verified ground truth
for these specific documents (only the TESTS carry expected numbers — the
parser itself fabricates nothing).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from argosy.services.payslip_parser import parse_payslip

_SAMPLE_DIR = Path(
    "D:/Google Drive/Family/Finances/Portfolio/Resources/2026/Payslip/Ariel"
)


def _path(mm: str) -> Path:
    return _SAMPLE_DIR / f"2026_{mm}.pdf"


def _have(mm: str) -> bool:
    return _path(mm).exists()


pytestmark = pytest.mark.skipif(
    not _SAMPLE_DIR.exists(),
    reason="Hilan payslip samples not present (Google Drive path).",
)


# ---------------------------------------------------------------------------
# Summary-6 ground truth, in document order:
#   total_payments, total_tax_deductions, provident_funds,
#   net_salary, obligation_deductions, net_to_pay
# ---------------------------------------------------------------------------
_SUMMARY = {
    "01": (59472.08, 24588.00, 3667.80, 31216.28, 8187.50, 23028.78),
    "02": (58795.68, 114186.00, 3667.80, -59058.12, -82237.02, 23178.90),
    "03": (64584.40, 104624.00, 3992.80, -44032.40, -68281.70, 24249.30),
    "04": (64594.20, 25611.00, 3992.80, 34990.40, 9000.00, 25990.40),
}

_VEST_MONTHS = {"02", "03"}


@pytest.mark.parametrize("mm", ["01", "02", "03", "04"])
def test_summary_six(mm: str) -> None:
    if not _have(mm):
        pytest.skip(f"2026_{mm}.pdf not present")
    f = parse_payslip(_path(mm))
    tp, ttd, pf, ns, od, ntp = _SUMMARY[mm]
    assert f.total_payments == pytest.approx(tp, abs=0.01)
    assert f.total_tax_deductions == pytest.approx(ttd, abs=0.01)
    assert f.provident_funds == pytest.approx(pf, abs=0.01)
    assert f.net_salary == pytest.approx(ns, abs=0.01)
    assert f.obligation_deductions == pytest.approx(od, abs=0.01)
    assert f.net_to_pay == pytest.approx(ntp, abs=0.01)


@pytest.mark.parametrize("mm", ["01", "02", "03", "04"])
def test_period(mm: str) -> None:
    if not _have(mm):
        pytest.skip(f"2026_{mm}.pdf not present")
    f = parse_payslip(_path(mm))
    assert f.period_year == 2026
    assert f.period_month == int(mm)
    assert f.confidence.get("period") == "high"


def test_tax_breakdown_2026_04() -> None:
    if not _have("04"):
        pytest.skip("2026_04.pdf not present")
    f = parse_payslip(_path("04"))
    assert f.income_tax == pytest.approx(19902.00, abs=0.01)
    assert f.national_insurance == pytest.approx(3175.00, abs=0.01)
    assert f.health_tax == pytest.approx(2534.00, abs=0.01)
    # The defining identity: the three components sum to total deductions.
    assert (
        f.income_tax + f.national_insurance + f.health_tax
        == pytest.approx(f.total_tax_deductions, abs=0.01)
    )
    # And the parser recognised that, marking them high-confidence.
    assert f.confidence["income_tax"] == "high"
    assert f.confidence["national_insurance"] == "high"
    assert f.confidence["health_tax"] == "high"


def test_monthly_context_2026_04() -> None:
    if not _have("04"):
        pytest.skip("2026_04.pdf not present")
    f = parse_payslip(_path("04"))
    assert f.gross_for_income_tax == pytest.approx(67626, abs=1)
    assert f.marginal_rate_pct == pytest.approx(50.0)
    assert f.credit_points == pytest.approx(4.25)


def test_ytd_rsu_fields_2026_04() -> None:
    """The key RSU-withholding YTD fields for 2026_04."""
    if not _have("04"):
        pytest.skip("2026_04.pdf not present")
    f = parse_payslip(_path("04"))
    assert f.ytd_regular_gross == pytest.approx(254593, abs=1)
    assert f.ytd_non_fixed_gross == pytest.approx(60679, abs=1)
    assert f.ytd_capital_gain == pytest.approx(549467, abs=1)
    assert f.ytd_regular_tax == pytest.approx(76693, abs=1)
    assert f.ytd_tax_on_non_fixed_gross == pytest.approx(167707, abs=1)
    assert f.ytd_taxable_income == pytest.approx(868285, abs=1)
    for k in (
        "ytd_non_fixed_gross",
        "ytd_capital_gain",
        "ytd_tax_on_non_fixed_gross",
    ):
        assert f.confidence[k] == "high"


def test_net_identity_holds_for_plain_month_04() -> None:
    """2026_04 is a plain month: the net-salary identity holds."""
    if not _have("04"):
        pytest.skip("2026_04.pdf not present")
    f = parse_payslip(_path("04"))
    net_calc = f.total_payments - f.total_tax_deductions - f.provident_funds
    assert net_calc == pytest.approx(f.net_salary, abs=0.01)
    # Cash identity too.
    assert (
        f.net_salary - f.obligation_deductions
        == pytest.approx(f.net_to_pay, abs=0.01)
    )
    assert f.confidence["net_salary"] == "high"
    assert f.confidence["net_to_pay"] == "high"
    # A plain month: positive book net, not flagged as a vest month.
    assert f.net_salary > 0
    assert f.is_vest_month is False
    # No identity-failure warnings for a plain month.
    assert not any("FAILED" in w for w in f.warnings)


@pytest.mark.parametrize("mm", sorted(_VEST_MONTHS))
def test_vest_month_flags_not_crashes(mm: str) -> None:
    """Vest months: parser FLAGS the equity event (does not crash).

    The accounting identities actually still HOLD in vest months (Hilan lets
    book net_salary go negative). The real, honest vest signal is therefore
    a negative book net_salary, which the parser surfaces as
    ``is_vest_month`` plus a warning.
    """
    if not _have(mm):
        pytest.skip(f"2026_{mm}.pdf not present")
    f = parse_payslip(_path(mm))
    # Still parses all summary numbers.
    assert f.total_payments is not None
    assert f.total_tax_deductions is not None
    # Book net is negative -> flagged as a vest/equity month.
    assert f.net_salary < 0
    assert f.is_vest_month is True
    assert any("Vest/equity month detected" in w for w in f.warnings)
    # All three identities STILL hold even in a vest month -> high confidence.
    assert f.confidence["net_salary"] == "high"
    assert f.confidence["net_to_pay"] == "high"
    assert (
        f.income_tax + f.national_insurance + f.health_tax
        == pytest.approx(f.total_tax_deductions, abs=0.01)
    )
    assert f.confidence["income_tax"] == "high"


def test_no_crash_and_warnings_is_list() -> None:
    if not _have("01"):
        pytest.skip("2026_01.pdf not present")
    f = parse_payslip(_path("01"))
    assert isinstance(f.warnings, list)
    assert isinstance(f.confidence, dict)
