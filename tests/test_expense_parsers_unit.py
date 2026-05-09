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
