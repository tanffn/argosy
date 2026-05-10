"""Tests for ``argosy.services.rsu_reconciliation``.

Three layers:
  1. ``parse_csv`` against a small synthetic CSV (1 Sale + 2 RS lots +
     1 Forced Disbursement + 1 unmodelled action).
  2. ``reconcile`` with synthetic SchwabReport + LeumiCredit lists —
     edge cases (empty Leumi, exact match, tolerance window, tie-break
     by amount distance).
  3. Live-fixture parse of the 2026 Schwab CSV under
     ``ARGOSY_EXPENSE_SAMPLES_ROOT`` (skipped if env var absent).
"""

from __future__ import annotations

import os
import textwrap
from datetime import date
from pathlib import Path

import pytest

from argosy.services.rsu_reconciliation import (
    LeumiCredit,
    SchwabDisbursement,
    SchwabReport,
    SchwabSale,
    parse_csv,
    reconcile,
)


# ---------------------------------------------------------------------------
# parse_csv
# ---------------------------------------------------------------------------


SYNTHETIC_CSV = textwrap.dedent('''\
"Date","Action","Symbol","Description","Quantity","FeesAndCommissions","DisbursementElection","Amount","Type","Shares","SalePrice","SubscriptionDate","SubscriptionFairMarketValue","PurchaseDate","PurchasePrice","PurchaseFairMarketValue","DispositionType","GrantId","VestDate","VestFairMarketValue","GrossProceeds","TotalCostBasis","RealizedGainLoss","HoldingPeriod","AwardDate","AwardId","FairMarketValuePrice","SharesSoldWithheldForTaxes","NetSharesDeposited","Taxes","TaxWithholdingMethod","SharesWithheld","SharesSold","CashRefund","CarryForward"
"04/21/2026","Forced Disbursement","NVDA","Debit","","","","-$207,538.02","","","","","","","","","","","","","","","","","","","","","","","","","","",""
"04/20/2026","Sale","NVDA","Share Sale","1040","$4.48","","$207,538.02","","","","","","","","","","","","","","","","","","","","","","","","","","",""
"","","","","","","","","RS","520","$199.5601","","","","","","","182406","12/13/2023","$48.088","","$25,005.76","$78,765.49","LONG TERM","","","","","","","","","","",""
"","","","","","","","","RS","520","$199.5601","","","","","","","182406","06/19/2024","$135.58","","$70,501.60","$33,269.65","LONG TERM","","","","","","","","","","",""
"04/15/2026","Adjustment","NVDA","Debit","","","","-$88.72","","","","","","","","","","","","","","","","","","","","","","","","","","",""
''')


def test_parse_csv_synthetic(tmp_path: Path) -> None:
    p = tmp_path / "schwab.csv"
    p.write_text(SYNTHETIC_CSV, encoding="utf-8")
    report = parse_csv(p)

    # 1 sale, 1 disbursement, 1 unparsed action.
    assert len(report.sales) == 1
    assert len(report.disbursements) == 1
    assert report.unparsed_actions == {"Adjustment": 1}

    sale = report.sales[0]
    assert sale.date == date(2026, 4, 20)
    assert sale.symbol == "NVDA"
    assert sale.quantity_shares == 1040
    assert sale.gross_usd == pytest.approx(207538.02)
    assert sale.fees_usd == pytest.approx(4.48)
    # No taxes column populated → 0.0; net = gross - fees.
    assert sale.total_taxes_usd == pytest.approx(0.0)
    assert sale.net_usd == pytest.approx(207533.54)
    assert len(sale.lots) == 2
    assert sale.lots[0].shares == 520
    assert sale.lots[0].sale_price_usd == pytest.approx(199.5601)
    assert sale.lots[0].vest_date == date(2023, 12, 13)
    # In the live CSV the GrossProceeds column is empty; the dollar values
    # land in TotalCostBasis (cost-basis at vest) and RealizedGainLoss.
    assert sale.lots[0].gross_proceeds_usd is None
    assert sale.lots[0].cost_basis_usd == pytest.approx(25005.76)
    assert sale.lots[0].realized_gain_usd == pytest.approx(78765.49)
    assert sale.lots[1].shares == 520
    assert sale.lots[1].vest_date == date(2024, 6, 19)

    disb = report.disbursements[0]
    assert disb.date == date(2026, 4, 21)
    assert disb.amount_usd == pytest.approx(207538.02)
    assert disb.action == "Forced Disbursement"


def test_parse_csv_empty(tmp_path: Path) -> None:
    """Header-only file → empty report, no exceptions."""
    header = ('"Date","Action","Symbol","Description","Quantity",'
              '"FeesAndCommissions","DisbursementElection","Amount","Type",'
              '"Shares","SalePrice","SubscriptionDate","SubscriptionFairMarketValue",'
              '"PurchaseDate","PurchasePrice","PurchaseFairMarketValue",'
              '"DispositionType","GrantId","VestDate","VestFairMarketValue",'
              '"GrossProceeds","TotalCostBasis","RealizedGainLoss","HoldingPeriod",'
              '"AwardDate","AwardId","FairMarketValuePrice",'
              '"SharesSoldWithheldForTaxes","NetSharesDeposited","Taxes",'
              '"TaxWithholdingMethod","SharesWithheld","SharesSold","CashRefund",'
              '"CarryForward"\n')
    p = tmp_path / "empty.csv"
    p.write_text(header, encoding="utf-8")
    report = parse_csv(p)
    assert report.sales == []
    assert report.disbursements == []
    assert report.unparsed_actions == {}


# ---------------------------------------------------------------------------
# reconcile
# ---------------------------------------------------------------------------


def _disb(d: date, amt: float) -> SchwabDisbursement:
    return SchwabDisbursement(date=d, amount_usd=amt, action="Forced Disbursement")


def _credit(tx_id: int, d: date, amt: float, ref: str = "X") -> LeumiCredit:
    return LeumiCredit(
        date=d, amount_usd=amt, merchant_raw="העברת כספים",
        reference=ref, tx_id=tx_id,
    )


def test_reconcile_exact_match() -> None:
    report = SchwabReport(disbursements=[
        _disb(date(2026, 4, 21), 207538.02),
    ])
    credits = [
        _credit(1, date(2026, 4, 23), 207538.02, "ref-A"),
    ]
    rec = reconcile(report, credits)
    assert len(rec.matches) == 1
    m = rec.matches[0]
    assert m.credit.tx_id == 1
    assert m.days_diff == 2
    assert m.amount_diff_usd == pytest.approx(0.0)
    assert rec.unmatched_disbursements == []
    assert rec.unmatched_leumi_credits == []
    assert "1/1 disbursements matched" in rec.summary


def test_reconcile_no_leumi_data() -> None:
    report = SchwabReport(disbursements=[_disb(date(2026, 4, 21), 207538.02)])
    rec = reconcile(report, [])
    assert rec.matches == []
    assert len(rec.unmatched_disbursements) == 1
    assert rec.unmatched_leumi_credits == []


def test_reconcile_outside_date_window() -> None:
    """Credit posted 20 days later → outside default 14-day window."""
    report = SchwabReport(disbursements=[_disb(date(2026, 4, 21), 100.00)])
    credits = [_credit(1, date(2026, 5, 11), 100.00)]   # +20 days
    rec = reconcile(report, credits)
    assert rec.matches == []
    assert len(rec.unmatched_disbursements) == 1
    assert len(rec.unmatched_leumi_credits) == 1


def test_reconcile_outside_amount_tolerance() -> None:
    """A 50%-of-disbursement credit is too small for either the exact-match
    tolerance or the haircut soft-match band → no match."""
    report = SchwabReport(disbursements=[_disb(date(2026, 4, 21), 100.00)])
    credits = [_credit(1, date(2026, 4, 23), 50.00)]  # 50% of disb — outside both bands
    rec = reconcile(report, credits)
    assert rec.matches == []


def test_reconcile_tiebreak_by_amount_distance() -> None:
    """Two candidates within window — closer-amount one wins."""
    report = SchwabReport(disbursements=[_disb(date(2026, 4, 21), 100.00)])
    credits = [
        _credit(10, date(2026, 4, 22), 100.50),  # $0.50 off, +1 day
        _credit(11, date(2026, 4, 23), 100.05),  # $0.05 off, +2 days
    ]
    rec = reconcile(report, credits, tolerance_usd=1.0)
    assert len(rec.matches) == 1
    # Closer amount wins, even though further in date.
    assert rec.matches[0].credit.tx_id == 11


def test_reconcile_credit_only_consumed_once() -> None:
    """One credit cannot satisfy two disbursements."""
    report = SchwabReport(disbursements=[
        _disb(date(2026, 4, 21), 100.00),
        _disb(date(2026, 4, 22), 100.00),
    ])
    credits = [_credit(1, date(2026, 4, 23), 100.00)]
    rec = reconcile(report, credits)
    assert len(rec.matches) == 1
    assert len(rec.unmatched_disbursements) == 1


# ---------------------------------------------------------------------------
# Haircut soft-match (Israeli capital-gains tax withholding ~28%)
# ---------------------------------------------------------------------------


def test_reconcile_haircut_pair_apr21() -> None:
    """The real-world Apr-2026 pair: Schwab disbursed $207,538.02 on
    2026-04-21, Leumi credited $150,864.02 eight days later (27.3% off the
    wire — consistent with IL CGT withholding)."""
    report = SchwabReport(disbursements=[
        _disb(date(2026, 4, 21), 207538.02),
    ])
    credits = [_credit(1, date(2026, 4, 29), 150864.02, "ref-A")]
    rec = reconcile(report, credits)
    assert len(rec.matches) == 1
    m = rec.matches[0]
    assert m.match_kind == "haircut"
    assert m.days_diff == 8
    # haircut_pct = (1 - 150864.02 / 207538.02) * 100 ≈ 27.3093...
    assert m.haircut_pct == pytest.approx(27.31, abs=0.02)
    # Signed amount: positive == bank received less than Schwab sent.
    assert m.amount_diff_usd == pytest.approx(56674.0, abs=1.0)
    assert rec.unmatched_disbursements == []
    assert rec.unmatched_leumi_credits == []


def test_reconcile_haircut_outside_range() -> None:
    """A 50%-of-disbursement credit is below the 60% floor → no haircut
    match. The credit returns to the unmatched pool intact."""
    report = SchwabReport(disbursements=[_disb(date(2026, 4, 21), 100.00)])
    credits = [_credit(1, date(2026, 4, 25), 50.00)]   # 50% of disb
    rec = reconcile(report, credits)
    assert rec.matches == []
    assert len(rec.unmatched_disbursements) == 1
    assert len(rec.unmatched_leumi_credits) == 1


def test_reconcile_existing_exact_match_still_works() -> None:
    """Exact pair stays exact: match_kind="exact", haircut_pct==0,
    amount_diff_usd≈0. Sign is now disb-credit but at parity that's zero."""
    report = SchwabReport(disbursements=[_disb(date(2026, 4, 21), 207538.02)])
    credits = [_credit(1, date(2026, 4, 23), 207538.02, "ref-A")]
    rec = reconcile(report, credits)
    assert len(rec.matches) == 1
    m = rec.matches[0]
    assert m.match_kind == "exact"
    assert m.haircut_pct == 0.0
    assert m.amount_diff_usd == pytest.approx(0.0)
    assert m.days_diff == 2


def test_reconcile_haircut_score_picks_closer_to_27_5_pct() -> None:
    """Two candidate haircuts (25% and 30%) — matcher picks 30% because
    its distance from the canonical 27.5% IL CGT rate is smaller."""
    disb_amt = 100_000.00
    report = SchwabReport(disbursements=[_disb(date(2026, 4, 21), disb_amt)])
    credits = [
        # 25% haircut → ratio 0.75 → distance from 27.5 = 2.5
        _credit(10, date(2026, 4, 27), disb_amt * 0.75, "ref-25"),
        # 30% haircut → ratio 0.70 → distance from 27.5 = 2.5 (tie)
        # Bump the 30% case slightly closer to 27.5 by using 0.705 (29.5%)
        _credit(11, date(2026, 4, 28), disb_amt * 0.705, "ref-30"),
    ]
    rec = reconcile(report, credits)
    assert len(rec.matches) == 1
    m = rec.matches[0]
    assert m.match_kind == "haircut"
    # 29.5% is 2.0 from 27.5; 25% is 2.5 from 27.5 — 29.5% wins.
    assert m.credit.tx_id == 11
    assert m.haircut_pct == pytest.approx(29.5, abs=0.05)


def test_reconcile_summary_includes_residual_credits() -> None:
    report = SchwabReport(disbursements=[_disb(date(2026, 4, 21), 100.00)])
    credits = [
        _credit(1, date(2026, 4, 23), 100.00),    # matches
        _credit(2, date(2026, 5, 10), 50.00),     # residual
    ]
    rec = reconcile(report, credits)
    assert len(rec.matches) == 1
    assert len(rec.unmatched_leumi_credits) == 1
    assert "1 Leumi credits unmatched" in rec.summary


# ---------------------------------------------------------------------------
# Live fixture (skipped when ARGOSY_EXPENSE_SAMPLES_ROOT not set)
# ---------------------------------------------------------------------------


def _samples_root() -> Path | None:
    root = os.environ.get("ARGOSY_EXPENSE_SAMPLES_ROOT")
    return Path(root) if root else None


@pytest.mark.skipif(
    _samples_root() is None
    or not _samples_root().exists()    # type: ignore[union-attr]
    or not (_samples_root() / "2026" / "Schwab" /     # type: ignore[union-attr]
            "EquityAwardsCenter_Transactions.csv").exists(),
    reason="ARGOSY_EXPENSE_SAMPLES_ROOT not set or 2026 Schwab CSV missing",
)
def test_parse_csv_live_fixture() -> None:
    root = _samples_root()
    assert root is not None
    p = root / "2026" / "Schwab" / "EquityAwardsCenter_Transactions.csv"
    report = parse_csv(p)
    assert report.sales, "no sales parsed from live CSV"
    assert report.disbursements, "no disbursements parsed from live CSV"
    # Each Sale must have at least one RS lot (Schwab always emits at
    # least one RS sub-row per Sale).
    for sale in report.sales:
        assert sale.lots, f"Sale on {sale.date} has no RS lots"
        assert sale.gross_usd > 0
        assert sale.quantity_shares > 0
