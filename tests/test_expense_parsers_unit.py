"""Per-issuer parser unit tests against synthetic fixtures."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "expenses"


def test_leumi_parser_returns_5_rows():
    from argosy.services.expense_ingest.parsers.leumi_osh import parse
    result = parse(FIXTURES / "leumi_osh_minimal.xls")
    assert len(result.transactions) == 5


def test_leumi_parser_separates_debits_and_credits():
    from argosy.services.expense_ingest.parsers.leumi_osh import parse
    result = parse(FIXTURES / "leumi_osh_minimal.xls")
    debits = [t for t in result.transactions if t.direction == "debit"]
    credits = [t for t in result.transactions if t.direction == "credit"]
    assert len(debits) == 3
    assert len(credits) == 2


def test_leumi_parser_keeps_card_payment_reference():
    from argosy.services.expense_ingest.parsers.leumi_osh import parse
    result = parse(FIXTURES / "leumi_osh_minimal.xls")
    card_pay = next(t for t in result.transactions
                    if "מאסטרקרד" in t.merchant_raw)
    assert card_pay.reference == "1266"
    assert card_pay.amount_nis == pytest.approx(3319.44)
    assert card_pay.direction == "debit"


def test_leumi_parser_normalizes_dash_yod_suffix():
    from argosy.services.expense_ingest.parsers.leumi_osh import parse
    result = parse(FIXTURES / "leumi_osh_minimal.xls")
    salary = next(t for t in result.transactions
                  if "מלאנוקס" in t.merchant_raw)
    assert "מלאנוקס טכנו-י" == salary.merchant_raw
    assert "מלאנוקס טכנו" == salary.merchant_normalized


def test_leumi_parser_statement_metadata():
    from argosy.services.expense_ingest.parsers.leumi_osh import parse
    result = parse(FIXTURES / "leumi_osh_minimal.xls")
    assert result.statement.period_start == date(2026, 4, 10)
    assert result.statement.period_end == date(2026, 5, 1)
    assert result.statement.declared_total_nis is None
    assert result.statement.charge_date is None


def test_isracard_parser_returns_5_rows():
    from argosy.services.expense_ingest.parsers.isracard import parse
    result = parse(FIXTURES / "isracard_minimal.xlsx")
    assert len(result.transactions) == 5


def test_isracard_parser_extracts_card_last4():
    from argosy.services.expense_ingest.parsers.isracard import parse
    result = parse(FIXTURES / "isracard_minimal.xlsx")
    assert result.source_hint is not None
    assert result.source_hint.kind == "card"
    assert result.source_hint.issuer == "isracard"
    assert result.source_hint.external_id == "1266"
    assert "אריאל" in result.source_hint.cardholder_name


def test_isracard_parser_charge_date():
    from argosy.services.expense_ingest.parsers.isracard import parse
    from datetime import date
    result = parse(FIXTURES / "isracard_minimal.xlsx")
    # The fixture says לחיוב ב-15.04 — year inferred from latest tx year
    assert result.statement.charge_date == date(2026, 4, 15)


def test_isracard_parser_handles_usd_row():
    from argosy.services.expense_ingest.parsers.isracard import parse
    result = parse(FIXTURES / "isracard_minimal.xlsx")
    usd = next(t for t in result.transactions
               if "NAME-CHEAP" in t.merchant_raw)
    assert usd.currency_orig == "USD"
    assert usd.amount_orig == 12.18
    # NIS-approximation must be set (we use a fallback constant in tests)
    assert usd.amount_nis > 0


def test_isracard_parser_detects_refund():
    from argosy.services.expense_ingest.parsers.isracard import parse
    result = parse(FIXTURES / "isracard_minimal.xlsx")
    refund = next(t for t in result.transactions
                  if "ZARA" in t.merchant_raw)
    assert refund.tx_type == "refund"
    assert refund.direction == "credit"
    assert refund.amount_nis == 50.0  # always positive on storage


def test_isracard_parser_detects_standing_order():
    from argosy.services.expense_ingest.parsers.isracard import parse
    result = parse(FIXTURES / "isracard_minimal.xlsx")
    netflix = next(t for t in result.transactions
                   if "NETFLIX" in t.merchant_raw)
    assert netflix.tx_type == "standing_order"


def test_max_parser_returns_5_rows():
    from argosy.services.expense_ingest.parsers.max import parse
    result = parse(FIXTURES / "max_minimal.xlsx")
    assert len(result.transactions) == 5


def test_max_parser_extracts_account_last4():
    from argosy.services.expense_ingest.parsers.max import parse
    result = parse(FIXTURES / "max_minimal.xlsx")
    assert result.source_hint is not None
    assert result.source_hint.issuer == "max"
    # Account is 882-44745280 → last 4 of the post-dash chunk = '5280'
    assert result.source_hint.external_id == "5280"


def test_max_parser_keeps_anaf_as_issuer_category():
    from argosy.services.expense_ingest.parsers.max import parse
    result = parse(FIXTURES / "max_minimal.xlsx")
    rest = next(t for t in result.transactions
                if "ספייס" in t.merchant_raw)
    assert rest.issuer_category == "מסעדות"


def test_max_parser_detects_refund():
    from argosy.services.expense_ingest.parsers.max import parse
    result = parse(FIXTURES / "max_minimal.xlsx")
    refund = next(t for t in result.transactions
                  if "WIZZ" in t.merchant_raw)
    assert refund.tx_type == "refund"
    assert refund.direction == "credit"
    assert refund.amount_nis == 2097.83  # always positive


def test_max_parser_charge_date_extracted():
    from argosy.services.expense_ingest.parsers.max import parse
    from datetime import date
    result = parse(FIXTURES / "max_minimal.xlsx")
    assert result.statement.charge_date == date(2026, 4, 15)
    assert abs(result.statement.declared_total_nis - 654.88) < 0.01
