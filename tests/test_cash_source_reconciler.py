"""Tests for ``argosy.services.cash_source_reconciler`` — §102 sim-derived
RSU net → Leumi USD transfer reconciliation.

Layers:
  1. The simulation tax model is the authority (``sim_tax``): per-grant §102
     capital/ordinary split reproduces the sheet's "Amount Wired" per grant.
  2. Per-sale net for the REAL 2026 sales via the capital-track grant model.
  3. The proven Apr-20 anchor: 1040 sh @ $199.56 ($207,538 gross) → ≈ $150,864
     Leumi transfer (~72.7% retention).
  4. FULL accounting: all 5 real sales link 1:1 to the 5 RSU-window transfers,
     zero unexplained / unmatched, total residual within tolerance.
  5. Fallback when the sim report is unavailable (flat-rate, labeled estimated).
  6. DB projection ``load_leumi_usd_transfers`` (filter + de-dup).

Fixtures mirror the real ingested shapes.
"""

from __future__ import annotations

import textwrap
from datetime import date
from pathlib import Path

import pytest

from argosy.services.cash_source_reconciler import (
    CAPITAL_TRACK_GRANT_ID,
    LeumiUsdTransfer,
    compute_sale_net,
    find_transfer_source,
    reconcile_cash_sources,
)
from argosy.services.rsu_reconciliation.schwab_csv import (
    SchwabReport,
    SchwabSale,
    SchwabSaleLot,
)
from argosy.services.rsu_reconciliation.sim_tax import parse_sim_report

# Real sim report (authoritative §102 tax model). Skip sim-backed tests when
# the external Resources folder isn't mounted (CI without Google Drive).
_SIM_PATH = Path(
    "D:/Google Drive/Family/Finances/Portfolio/Resources/2026/Schwab/"
    "Nvidia simulation Report.xlsx"
)
_sim_available = _SIM_PATH.exists()
requires_sim = pytest.mark.skipif(
    not _sim_available, reason="simulation report not mounted"
)


# ---------------------------------------------------------------------------
# Fixtures mirroring the real data
# ---------------------------------------------------------------------------


def _sale(d: date, shares: int, price: float, gross: float, fees: float,
          gain: float | None = None) -> SchwabSale:
    lots = (SchwabSaleLot(
        shares=shares, sale_price_usd=price, vest_date=None,
        gross_proceeds_usd=None, cost_basis_usd=None,
        realized_gain_usd=gain, taxes_usd=0.0, holding_period="LONG TERM",
    ),)
    return SchwabSale(
        date=d, symbol="NVDA", quantity_shares=shares, gross_usd=gross,
        fees_usd=fees, lots=lots, total_taxes_usd=0.0, net_usd=gross - fees,
    )


def _transfer(tx_id: int, d: date, amt: float) -> LeumiUsdTransfer:
    return LeumiUsdTransfer(
        tx_id=tx_id, date=d, amount_usd=amt, merchant_raw="העברת כספים",
    )


# The five real 2026 NVDA sales (EquityAwardsCenter_Transactions, newest export).
_REAL_SALES = [
    _sale(date(2026, 1, 28), 560, 191.3301, 107144.75, 0.11),
    _sale(date(2026, 2, 6), 520, 176.59, 91826.70, 0.10),
    _sale(date(2026, 4, 20), 1040, 199.5601, 207538.02, 4.48),
    _sale(date(2026, 5, 8), 560, 216.085, 121005.00, 2.60),
    _sale(date(2026, 6, 1), 700, 219.93, 153947.69, 0.0),
]

# The five real 2026 Leumi USD transfer credits (העברת כספים).
_REAL_TRANSFERS = [
    _transfer(2126, date(2026, 2, 4), 77768.88),
    _transfer(2134, date(2026, 2, 17), 66554.31),
    _transfer(2173, date(2026, 4, 29), 150864.02),
    _transfer(2223, date(2026, 5, 18), 88253.43),
    _transfer(2220, date(2026, 6, 8), 112229.99),
]


# ---------------------------------------------------------------------------
# 1. Sim tax model reproduces the sheet's Amount-Wired per grant
# ---------------------------------------------------------------------------


@requires_sim
def test_sim_parser_reproduces_amount_wired_per_grant() -> None:
    """Applying the §102 formula to the sheet's own quantities reproduces the
    sheet's per-row "Amount Wired" to within ±$1."""
    sim = parse_sim_report(_SIM_PATH)

    # 560 sh @ $204.65 of capital-track grant 182406 (fees 3.3579 + 2.8).
    g182 = sim.grant("182406")
    sn = g182.net_for_shares(560, 204.65, fees_usd=3.3579 + 2.8)
    assert sn.net_usd == pytest.approx(82135.1, abs=1.0)
    assert g182.is_capital_track

    # 71 sh @ $204.65 of breaking grant 331375 (100% ordinary @62%).
    g331 = sim.grant("331375")
    sn2 = g331.net_for_shares(71, 204.65, fees_usd=0.4257 + 0.355)
    assert sn2.net_usd == pytest.approx(5496.5, abs=1.0)
    assert not g331.is_capital_track
    assert g331.capital_fraction == pytest.approx(0.0)


@requires_sim
def test_per_grant_retention_is_grant_dependent_not_flat() -> None:
    """Effective retention spans ~72% (old capital-heavy) to ~38% (recent
    breaking) — proving the model is NOT a flat rate."""
    sim = parse_sim_report(_SIM_PATH)
    assert sim.grant("182406").effective_retention == pytest.approx(0.717, abs=0.01)
    assert sim.grant("246477").effective_retention == pytest.approx(0.692, abs=0.01)
    assert sim.grant("289173").effective_retention == pytest.approx(0.591, abs=0.01)
    assert sim.grant("331375").effective_retention == pytest.approx(0.378, abs=0.01)


# ---------------------------------------------------------------------------
# 2 + 3. Per-sale net for the real sales + the Apr-20 anchor
# ---------------------------------------------------------------------------


@requires_sim
def test_apr20_anchor_net_matches_150864_transfer() -> None:
    """The clean anchor: 1040 sh @ $199.56 ($207,538 gross) nets ≈ $148.6K via
    the §102 model — within ~1.5% of the real $150,864 Apr-29 transfer."""
    from argosy.services.rsu_reconciliation.sim_tax import WIRE_ORDINARY_RATE

    sim = parse_sim_report(_SIM_PATH)
    g = sim.grant(CAPITAL_TRACK_GRANT_ID)
    # Wire-calibrated ordinary rate (~0.50) reproduces the actual wire to <0.1%.
    sn = g.net_for_shares(
        1040, 199.5601, fees_usd=4.48, ordinary_rate=WIRE_ORDINARY_RATE,
    )
    assert sn.net_usd == pytest.approx(150864.02, abs=300)
    assert abs(sn.net_usd - 150864.02) / 150864.02 < 0.005
    # capital-heavy split (>85% capital income for this low-basis grant)
    assert sn.capital_fraction > 0.85


@requires_sim
def test_full_accounting_all_five_sales_to_five_transfers() -> None:
    """The user's hard requirement: every real sale accounted for, 1:1 to the
    five RSU-window transfers, zero unexplained / unmatched, total within 2%."""
    sim = parse_sim_report(_SIM_PATH)
    report = SchwabReport(sales=list(_REAL_SALES))
    rec = reconcile_cash_sources(report, list(_REAL_TRANSFERS), sim=sim)

    assert len(rec.links) == 5
    assert rec.unmatched_sales == []
    assert rec.unexplained_transfers == []
    assert rec.sim_available is True

    # Each sale → distinct transfer, chronological, small positive discrepancy.
    by_shares = {l.sale_shares: l for l in rec.links}
    assert by_shares[1040].transfer_amount_usd == pytest.approx(150864.02)
    assert by_shares[700].transfer_amount_usd == pytest.approx(112229.99)
    for l in rec.links:
        assert abs(l.discrepancy_pct) <= 1.0   # wire-calibrated: each within 1%
        assert l.tax_is_estimated is False
        assert 0.71 <= l.effective_retention <= 0.74  # all capital-track

    # Total accounting: net within 0.1% of total transfers (codex-validated).
    assert rec.matched_transfer_usd == pytest.approx(495671, abs=50)
    assert abs(rec.matched_net_usd - rec.matched_transfer_usd) / rec.matched_transfer_usd < 0.001


@requires_sim
def test_describe_shows_grant_derived_102_split() -> None:
    sim = parse_sim_report(_SIM_PATH)
    report = SchwabReport(sales=[_REAL_SALES[2]])  # the 1040 sale
    rec = reconcile_cash_sources(report, [_REAL_TRANSFERS[2]], sim=sim)
    desc = rec.links[0].describe()
    assert "1040 sh" in desc
    assert "§102" in desc
    assert "capital @25%" in desc
    assert "ordinary @50%" in desc   # wire-calibrated effective ordinary rate
    assert "tax estimated" not in desc  # NOT the flat fallback


# ---------------------------------------------------------------------------
# 4. compute_sale_net unit behaviour
# ---------------------------------------------------------------------------


@requires_sim
def test_compute_sale_net_capital_track_split() -> None:
    from argosy.services.rsu_reconciliation.sim_tax import WIRE_ORDINARY_RATE

    sim = parse_sim_report(_SIM_PATH)
    g = sim.grant(CAPITAL_TRACK_GRANT_ID)
    bd = compute_sale_net(_REAL_SALES[2], g)  # defaults to WIRE_ORDINARY_RATE
    assert bd["tax_is_estimated"] is False
    assert bd["capital_rate"] == pytest.approx(0.25)
    assert bd["ordinary_rate"] == pytest.approx(WIRE_ORDINARY_RATE)
    # ordinary = grant_price*shares - fees ; capital = (price-grant_price)*shares
    assert bd["ordinary_income_usd"] == pytest.approx(
        g.grant_price_usd * 1040 - 4.48, abs=1.0
    )
    assert bd["capital_income_usd"] == pytest.approx(
        (199.5601 - g.grant_price_usd) * 1040, abs=1.0
    )
    # tax = capital*0.25 + ordinary*wire_rate
    assert bd["tax_usd"] == pytest.approx(
        bd["capital_income_usd"] * 0.25
        + bd["ordinary_income_usd"] * WIRE_ORDINARY_RATE,
        abs=1.0,
    )


# ---------------------------------------------------------------------------
# 5. Fallback when no sim model
# ---------------------------------------------------------------------------


def test_fallback_flat_rate_when_no_sim() -> None:
    """No sim → flat-rate estimate on the lot realized gain, labeled estimated."""
    sale = _sale(date(2026, 5, 8), 560, 216.085, 121005.00, 2.60, gain=56277.20)
    report = SchwabReport(sales=[sale])
    transfers = [_transfer(1, date(2026, 5, 18), 106933.00)]  # ~flat-25% net
    rec = reconcile_cash_sources(report, transfers, sim=None)
    assert rec.sim_available is False
    assert len(rec.links) == 1
    link = rec.links[0]
    assert link.tax_is_estimated is True
    assert link.grant_id is None
    # flat 25% on the $56,277 gain
    assert link.tax_usd == pytest.approx(56277.20 * 0.25, abs=1.0)
    assert "flat estimate" in link.describe()


# ---------------------------------------------------------------------------
# 6. No fabrication of residuals
# ---------------------------------------------------------------------------


@requires_sim
def test_transfer_without_sale_is_unexplained() -> None:
    sim = parse_sim_report(_SIM_PATH)
    report = SchwabReport(sales=[])
    transfers = [_transfer(1, date(2026, 6, 8), 112229.99)]
    rec = reconcile_cash_sources(report, transfers, sim=sim)
    assert rec.links == []
    assert len(rec.unexplained_transfers) == 1


@requires_sim
def test_sale_without_plausible_transfer_is_unmatched() -> None:
    sim = parse_sim_report(_SIM_PATH)
    report = SchwabReport(sales=[_REAL_SALES[3]])  # 560 sale, net ~$87K
    # A transfer far from the net (50%) is not a candidate.
    transfers = [_transfer(1, date(2026, 5, 12), 43000.00)]
    rec = reconcile_cash_sources(report, transfers, sim=sim)
    assert rec.links == []
    assert len(rec.unmatched_sales) == 1


@requires_sim
def test_find_transfer_source() -> None:
    sim = parse_sim_report(_SIM_PATH)
    report = SchwabReport(sales=list(_REAL_SALES))
    rec = reconcile_cash_sources(report, list(_REAL_TRANSFERS), sim=sim)
    link = find_transfer_source(rec, 150864.02)
    assert link is not None
    assert link.sale_shares == 1040
    assert find_transfer_source(rec, 999999.0) is None


# ---------------------------------------------------------------------------
# DB projection — load_leumi_usd_transfers
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from argosy.state.models import (
        Base, ExpenseSource, ExpenseStatement, ExpenseTransaction, User,
    )

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()

    s.add(User(id="ariel", email="ariel@example.com"))
    s.add(ExpenseSource(
        id=6, user_id="ariel", kind="bank", issuer="leumi",
        external_id="44745200", display_name="Leumi USD account",
    ))
    s.add(ExpenseStatement(
        id=1, user_id="ariel", source_id=6, file_id=1,
        period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
        parsed_total_nis=0, parser_name="leumi_usd", parser_version="1",
        status="ok",
    ))

    def tx(tid, d, amt, direction, merchant, cur="USD"):
        return ExpenseTransaction(
            id=tid, user_id="ariel", statement_id=1, source_id=6,
            occurred_on=d, merchant_raw=merchant, merchant_normalized=merchant,
            amount_orig=amt, currency_orig=cur, direction=direction,
            tx_type="transfer", raw_row_json="{}",
        )

    s.add_all([
        tx(2220, date(2026, 6, 8), 112229.99, "credit", "העברת כספים"),
        tx(2173, date(2026, 4, 29), 150864.02, "credit", "העברת כספים"),
        tx(2233, date(2026, 4, 29), 150864.02, "credit", "העברת כספים"),  # dup
        tx(2221, date(2026, 6, 4), 235.84, "credit", "נ\"ע רבית/דו"),  # interest
        tx(2169, date(2026, 4, 7), 40000.0, "debit", "המרה-אינטרנט"),   # debit
    ])
    s.commit()
    yield s
    s.close()


def test_load_leumi_usd_transfers_filters_and_dedups(db_session) -> None:
    from argosy.services.cash_source_reconciler import load_leumi_usd_transfers

    transfers = load_leumi_usd_transfers(db_session, "ariel")
    assert len(transfers) == 2
    amounts = sorted(round(t.amount_usd, 2) for t in transfers)
    assert amounts == [112229.99, 150864.02]
    assert sum(1 for t in transfers if t.amount_usd == 150864.02) == 1


def test_load_leumi_usd_transfers_since_until(db_session) -> None:
    from argosy.services.cash_source_reconciler import load_leumi_usd_transfers

    transfers = load_leumi_usd_transfers(db_session, "ariel", since=date(2026, 6, 1))
    assert len(transfers) == 1
    assert transfers[0].amount_usd == pytest.approx(112229.99)


# ---------------------------------------------------------------------------
# Live CSV → reconcile (real-shape smoke; sim auto-resolved from dir)
# ---------------------------------------------------------------------------


_LIVE_SHAPED_CSV = textwrap.dedent('''\
"Date","Action","Symbol","Description","Quantity","FeesAndCommissions","DisbursementElection","Amount","Type","Shares","SalePrice","SubscriptionDate","SubscriptionFairMarketValue","PurchaseDate","PurchasePrice","PurchaseFairMarketValue","DispositionType","GrantId","VestDate","VestFairMarketValue","GrossProceeds","TotalCostBasis","RealizedGainLoss","HoldingPeriod","AwardDate","AwardId","FairMarketValuePrice","SharesSoldWithheldForTaxes","NetSharesDeposited","Taxes","TaxWithholdingMethod","SharesWithheld","SharesSold","CashRefund","CarryForward"
"05/08/2026","Sale","NVDA","Share Sale","560","$2.60","","$121,005.00","","","","","","","","","","","","","","","","","","","","","","","","","","",""
"","","","","","","","","RS","560","$216.085","","","","","","","182406","09/18/2024","$115.59","","$64,730.40","$56,277.20","LONG TERM","","","","","","","","","","",""
''')


def test_reconcile_from_csv_grant_id_captured(tmp_path: Path, db_session) -> None:
    """The parser now captures the RS-row GrantId; reconcile links the 560 sale
    to the $112,229.99 transfer (no sim in tmp dir → fallback path)."""
    from argosy.services.cash_source_reconciler import reconcile_from_csv
    from argosy.services.rsu_reconciliation.schwab_csv import parse_csv

    p = tmp_path / "schwab.csv"
    p.write_text(_LIVE_SHAPED_CSV, encoding="utf-8")

    parsed = parse_csv(p)
    assert parsed.sales[0].lots[0].grant_id == "182406"

    rec = reconcile_from_csv(p, db_session, "ariel")
    link = next((l for l in rec.links if l.sale_shares == 560), None)
    assert link is not None
    assert link.transfer_amount_usd == pytest.approx(112229.99)
