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
    # Bug 2 (part 1): foreign rows must NOT carry a NIS amount — downstream
    # FX conversion (argosy.services.fx) is responsible for NIS-equivalent
    # rendering. Storing the raw foreign amount as `amount_nis` was the bug.
    assert usd.amount_nis is None


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
    """Bug 1 fallback path: when no last4_hint is provided, the parser falls
    back to the bank-account last-4 from the sheet name AND emits a warning
    so callers know they're getting the wrong identifier (the bank-account
    last-4, not the card last-4 — Max files only carry the bank account).
    """
    import warnings
    from argosy.services.expense_ingest.parsers.max import parse
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = parse(FIXTURES / "max_minimal.xlsx")
    assert result.source_hint is not None
    assert result.source_hint.issuer == "max"
    # Fallback: account is 882-44745280 → last 4 of the post-dash chunk = '5280'
    assert result.source_hint.external_id == "5280"
    assert any("last4_hint" in str(w.message) for w in caught), (
        "expected a UserWarning mentioning 'last4_hint'"
    )


def test_max_parser_uses_last4_hint():
    """Bug 1 happy path: when last4_hint is provided, the parser uses it as
    the source's external_id (the actual card last-4).
    """
    from argosy.services.expense_ingest.parsers.max import parse
    result = parse(FIXTURES / "max_minimal.xlsx", last4_hint="6225")
    assert result.source_hint is not None
    assert result.source_hint.issuer == "max"
    assert result.source_hint.external_id == "6225"


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


# ---------------------------------------------------------------------------
# Discount Bank Mastercard parser tests
# ---------------------------------------------------------------------------

def test_discount_parser_reads_both_sheets():
    from argosy.services.expense_ingest.parsers.discount import parse
    result = parse(FIXTURES / "discount_minimal.xlsx")
    # 5 domestic + 2 foreign = 7 rows
    assert len(result.transactions) == 7


def test_discount_parser_extracts_card_last4():
    from argosy.services.expense_ingest.parsers.discount import parse
    result = parse(FIXTURES / "discount_minimal.xlsx")
    assert result.source_hint is not None
    assert result.source_hint.issuer == "discount"
    assert result.source_hint.external_id == "2923"


def test_discount_parser_preserves_kategoria():
    from argosy.services.expense_ingest.parsers.discount import parse
    result = parse(FIXTURES / "discount_minimal.xlsx")
    rest = next(t for t in result.transactions
                if "T C" in t.merchant_raw)
    assert rest.issuer_category == "מסעדות, קפה וברים"


def test_discount_parser_detects_refund_by_negative():
    from argosy.services.expense_ingest.parsers.discount import parse
    result = parse(FIXTURES / "discount_minimal.xlsx")
    refund = next(t for t in result.transactions
                  if "ALIEXPRESS" in t.merchant_raw and abs(t.amount_nis - 25.31) < 0.01)
    assert refund.tx_type == "refund"
    assert refund.direction == "credit"


def test_discount_parser_no_charge_date_metadata():
    from argosy.services.expense_ingest.parsers.discount import parse
    result = parse(FIXTURES / "discount_minimal.xlsx")
    assert result.statement.charge_date is None
    assert result.statement.declared_total_nis is None
