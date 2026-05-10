"""HTTP tests for ``GET /api/expenses/rsu-reconciliation``.

Two scenarios:
  1. ``ARGOSY_EXPENSE_SAMPLES_ROOT`` points at a tiny synthetic Schwab CSV
     and we have a Leumi USD credit row in the DB that should pair with
     the disbursement → response carries both sides + a populated match.
  2. ``ARGOSY_EXPENSE_SAMPLES_ROOT`` is unset → response is 200 with empty
     lists and a warning string (graceful degradation, not 4xx).
"""

from __future__ import annotations

import textwrap
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from argosy.state.models import (
    ExpenseSource,
    ExpenseStatement,
    ExpenseTransaction,
    User,
    UserFile,
)


# Synthetic CSV with one Sale (2 RS lots), one matching Forced Disbursement,
# and one unmodelled Adjustment row. Mirrors test_rsu_reconciliation.py.
SYNTHETIC_CSV = textwrap.dedent('''\
"Date","Action","Symbol","Description","Quantity","FeesAndCommissions","DisbursementElection","Amount","Type","Shares","SalePrice","SubscriptionDate","SubscriptionFairMarketValue","PurchaseDate","PurchasePrice","PurchaseFairMarketValue","DispositionType","GrantId","VestDate","VestFairMarketValue","GrossProceeds","TotalCostBasis","RealizedGainLoss","HoldingPeriod","AwardDate","AwardId","FairMarketValuePrice","SharesSoldWithheldForTaxes","NetSharesDeposited","Taxes","TaxWithholdingMethod","SharesWithheld","SharesSold","CashRefund","CarryForward"
"04/21/2026","Forced Disbursement","NVDA","Debit","","","","-$207,538.02","","","","","","","","","","","","","","","","","","","","","","","","","","",""
"04/20/2026","Sale","NVDA","Share Sale","1040","$4.48","","$207,538.02","","","","","","","","","","","","","","","","","","","","","","","","","","",""
"","","","","","","","","RS","520","$199.5601","","","","","","","182406","12/13/2023","$48.088","","$25,005.76","$78,765.49","LONG TERM","","","","","","","","","","",""
"","","","","","","","","RS","520","$199.5601","","","","","","","182406","06/19/2024","$135.58","","$70,501.60","$33,269.65","LONG TERM","","","","","","","","","","",""
"04/15/2026","Adjustment","NVDA","Debit","","","","-$88.72","","","","","","","","","","","","","","","","","","","","","","","","","","",""
''')


def _seed_user_and_leumi_credit(
    SessionFactory,
    *,
    user_id: str = "ariel",
    occurred_on: date = date(2026, 4, 23),
    amount_usd: float = 207538.02,
    reference: str = "ref-A",
) -> int:
    """Seed a User + Leumi USD source/statement + one credit transaction.

    Returns the inserted transaction id.
    """
    with SessionFactory() as s:
        if s.get(User, user_id) is None:
            s.add(User(id=user_id, plan="free")); s.flush()
        f = UserFile(
            user_id=user_id, sha256="r" * 64,
            original_name="leumi_usd.xlsx", sanitized_name="leumi_usd.xlsx",
            mime_type="x", kind="other", size_bytes=1,
            storage_path="/tmp/leumi_usd.xlsx", source="chat_attachment",
        )
        s.add(f); s.flush()
        src = ExpenseSource(
            user_id=user_id, kind="bank_account", issuer="leumi",
            external_id="44745200", display_name="Leumi USD",
        )
        s.add(src); s.flush()
        stmt = ExpenseStatement(
            user_id=user_id, source_id=src.id, file_id=f.id,
            period_start=date(2026, 4, 1), period_end=date(2026, 4, 30),
            parsed_total_nis=Decimal("0"), parser_name="leumi_usd",
            parser_version="0.1.0", status="parsed",
        )
        s.add(stmt); s.flush()
        tx = ExpenseTransaction(
            user_id=user_id, source_id=src.id, statement_id=stmt.id,
            occurred_on=occurred_on,
            merchant_raw="העברת כספים",
            merchant_normalized="ha avarat ksapim",
            amount_nis=None,
            amount_orig=Decimal(str(amount_usd)),
            currency_orig="USD",
            direction="credit", tx_type="regular",
            reference=reference,
            raw_row_json="{}",
        )
        s.add(tx); s.commit()
        return tx.id


def test_rsu_reconciliation_with_synthetic_csv_pairs_disbursement(
    client_with_db, tmp_path: Path, monkeypatch,
):
    """Synthetic Schwab CSV + matching Leumi credit → endpoint surfaces both
    sides and the disbursement carries a non-null matched_leumi_credit_id."""
    # 1. Lay out <root>/2026/Schwab/<file>.csv
    samples_root = tmp_path / "samples"
    schwab_dir = samples_root / "2026" / "Schwab"
    schwab_dir.mkdir(parents=True)
    csv_path = schwab_dir / "EquityAwardsCenter_Transactions.csv"
    csv_path.write_text(SYNTHETIC_CSV, encoding="utf-8")
    monkeypatch.setenv("ARGOSY_EXPENSE_SAMPLES_ROOT", str(samples_root))

    # 2. Seed Leumi USD credit that should pair with the synthetic disbursement.
    SessionFactory = client_with_db.app.state.session_factory
    leumi_tx_id = _seed_user_and_leumi_credit(SessionFactory)

    # 3. Hit endpoint.
    r = client_with_db.get(
        "/api/expenses/rsu-reconciliation",
        params={"user_id": "ariel"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # ---- sales: 1 sale, 2 lots ----
    assert len(body["sales"]) == 1
    sale = body["sales"][0]
    assert sale["date"] == "2026-04-20"
    assert sale["symbol"] == "NVDA"
    assert sale["quantity_shares"] == 1040
    assert sale["gross_usd"] == pytest.approx(207538.02)
    assert sale["fees_usd"] == pytest.approx(4.48)
    assert len(sale["lots"]) == 2
    assert all(lot["holding_period"] == "LONG TERM" for lot in sale["lots"])

    # ---- disbursements: 1, paired ----
    assert len(body["disbursements"]) == 1
    disb = body["disbursements"][0]
    assert disb["date"] == "2026-04-21"
    assert disb["amount_usd"] == pytest.approx(207538.02)
    assert disb["matched_leumi_credit_id"] == leumi_tx_id
    assert disb["days_diff"] == 2
    assert disb["amount_diff_usd"] == pytest.approx(0.0)

    # ---- leumi_credits: 1, paired back to disbursement index 0 ----
    assert len(body["leumi_credits"]) == 1
    cr = body["leumi_credits"][0]
    assert cr["tx_id"] == leumi_tx_id
    assert cr["matched_disbursement_index"] == 0
    assert cr["merchant_raw"] == "העברת כספים"

    # ---- summary ----
    s = body["summary"]
    assert s["sales_count"] == 1
    assert s["disbursements_count"] == 1
    assert s["disbursements_matched_count"] == 1
    assert s["leumi_credits_count"] == 1
    assert s["leumi_credits_unmatched_count"] == 0

    # ---- meta ----
    assert body["warning"] is None
    assert len(body["schwab_csv_paths"]) == 1
    assert body["schwab_csv_paths"][0].endswith(
        "EquityAwardsCenter_Transactions.csv"
    )


def test_rsu_reconciliation_filters_leumi_credits_to_wire_transfers_only(
    client_with_db, tmp_path: Path, monkeypatch,
):
    """Only ``העברת כספים`` Leumi credits are returned; dividend/interest rows
    (e.g. ``נ"ע רבית/דו``) must be excluded so the unmatched-credit count
    stays meaningful for the RSU view."""
    samples_root = tmp_path / "samples"
    schwab_dir = samples_root / "2026" / "Schwab"
    schwab_dir.mkdir(parents=True)
    (schwab_dir / "EquityAwardsCenter_Transactions.csv").write_text(
        SYNTHETIC_CSV, encoding="utf-8",
    )
    monkeypatch.setenv("ARGOSY_EXPENSE_SAMPLES_ROOT", str(samples_root))

    SessionFactory = client_with_db.app.state.session_factory
    # Wire-transfer credit (matches the synthetic disbursement).
    wire_tx_id = _seed_user_and_leumi_credit(SessionFactory)
    # Dividend / interest credit — must be filtered out.
    with SessionFactory() as s:
        src = (
            s.query(ExpenseSource)
            .filter_by(user_id="ariel", external_id="44745200")
            .one()
        )
        stmt = s.query(ExpenseStatement).filter_by(source_id=src.id).one()
        dividend = ExpenseTransaction(
            user_id="ariel", source_id=src.id, statement_id=stmt.id,
            occurred_on=date(2026, 4, 25),
            merchant_raw='נ"ע רבית/דו',
            merchant_normalized='nv ravit/du',
            amount_nis=None,
            amount_orig=Decimal("12.34"),
            currency_orig="USD",
            direction="credit", tx_type="regular",
            reference="div-1",
            raw_row_json="{}",
        )
        s.add(dividend); s.commit()

    r = client_with_db.get(
        "/api/expenses/rsu-reconciliation",
        params={"user_id": "ariel"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["leumi_credits"]) == 1
    assert body["leumi_credits"][0]["tx_id"] == wire_tx_id
    assert body["leumi_credits"][0]["merchant_raw"] == "העברת כספים"
    # Summary count and unmatched count both reflect the filtered set.
    assert body["summary"]["leumi_credits_count"] == 1
    assert body["summary"]["leumi_credits_unmatched_count"] == 0


def test_rsu_reconciliation_surfaces_pending_sales_without_disbursement(
    client_with_db, tmp_path: Path, monkeypatch,
):
    """A Sale without a matching Forced Disbursement in the next 14 days
    should land in ``pending_sales`` (and the disbursed sale should NOT)."""
    # Two sales — one with a matching disbursement next day, one without.
    # The unmatched sale ($121,005 net) is what the user wants to see in
    # the pending bucket, mirroring the real 2026-05-08 NVDA sale.
    csv = textwrap.dedent('''\
"Date","Action","Symbol","Description","Quantity","FeesAndCommissions","DisbursementElection","Amount","Type","Shares","SalePrice","SubscriptionDate","SubscriptionFairMarketValue","PurchaseDate","PurchasePrice","PurchaseFairMarketValue","DispositionType","GrantId","VestDate","VestFairMarketValue","GrossProceeds","TotalCostBasis","RealizedGainLoss","HoldingPeriod","AwardDate","AwardId","FairMarketValuePrice","SharesSoldWithheldForTaxes","NetSharesDeposited","Taxes","TaxWithholdingMethod","SharesWithheld","SharesSold","CashRefund","CarryForward"
"05/08/2026","Sale","NVDA","Share Sale","600","$5.00","","$121,005.00","","","","","","","","","","","","","","","","","","","","","","","","","","",""
"","","","","","","","","RS","600","$201.6750","","","","","","","182406","12/13/2023","$48.088","","$121,005.00","$28,852.80","$92,152.20","LONG TERM","","","","","","","","","","",""
"04/21/2026","Forced Disbursement","NVDA","Debit","","","","-$207,538.02","","","","","","","","","","","","","","","","","","","","","","","","","","",""
"04/20/2026","Sale","NVDA","Share Sale","1040","$4.48","","$207,538.02","","","","","","","","","","","","","","","","","","","","","","","","","","",""
"","","","","","","","","RS","1040","$199.5601","","","","","","","182406","12/13/2023","$48.088","","$207,538.02","$50,011.52","$157,526.50","LONG TERM","","","","","","","","","","",""
''')

    samples_root = tmp_path / "samples"
    schwab_dir = samples_root / "2026" / "Schwab"
    schwab_dir.mkdir(parents=True)
    (schwab_dir / "EquityAwardsCenter_Transactions.csv").write_text(
        csv, encoding="utf-8",
    )
    monkeypatch.setenv("ARGOSY_EXPENSE_SAMPLES_ROOT", str(samples_root))

    SessionFactory = client_with_db.app.state.session_factory
    _seed_user_and_leumi_credit(SessionFactory)

    r = client_with_db.get(
        "/api/expenses/rsu-reconciliation",
        params={"user_id": "ariel"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # ---- pending_sales: only the 2026-05-08 sale (no disbursement yet) ----
    assert "pending_sales" in body
    assert len(body["pending_sales"]) == 1
    pending = body["pending_sales"][0]
    assert pending["date"] == "2026-05-08"
    assert pending["quantity_shares"] == 600
    assert pending["gross_usd"] == pytest.approx(121005.00)
    # net = gross - fees (no employer taxes withheld at lot level here)
    assert pending["net_usd"] == pytest.approx(121000.00)
    # days_since_sale must be >= 0 (today is past 2026-05-08).
    assert pending["days_since_sale"] >= 0

    # The April 20 sale DID have a disbursement on April 21 → not pending.
    pending_dates = {p["date"] for p in body["pending_sales"]}
    assert "2026-04-20" not in pending_dates

    # Summary mirrors the list.
    s = body["summary"]
    assert s["pending_sales_count"] == 1
    assert s["pending_sales_total_gross_usd"] == pytest.approx(121005.00)


def test_rsu_reconciliation_without_env_var_returns_warning_and_empty(
    client_with_db, monkeypatch,
):
    """Env var unset → 200 with warning + empty data lists (graceful)."""
    monkeypatch.delenv("ARGOSY_EXPENSE_SAMPLES_ROOT", raising=False)

    # Seed a User row so the route's join doesn't 500 on FK enforcement.
    SessionFactory = client_with_db.app.state.session_factory
    with SessionFactory() as s:
        if s.get(User, "ariel") is None:
            s.add(User(id="ariel", plan="free")); s.commit()

    r = client_with_db.get(
        "/api/expenses/rsu-reconciliation",
        params={"user_id": "ariel"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["warning"] is not None
    assert "ARGOSY_EXPENSE_SAMPLES_ROOT" in body["warning"]
    assert body["sales"] == []
    assert body["disbursements"] == []
    assert body["leumi_credits"] == []
    assert body["schwab_csv_paths"] == []
    assert body["summary"]["sales_count"] == 0
    assert body["summary"]["disbursements_count"] == 0
