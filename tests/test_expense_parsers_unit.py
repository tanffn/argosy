"""Per-issuer parser unit tests against synthetic fixtures."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "expenses"

_SAMPLES = os.environ.get("ARGOSY_EXPENSE_SAMPLES_ROOT")


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


def test_normalize_currency_maps_symbols_to_iso():
    from argosy.services.expense_ingest.parsers.isracard import _normalize_currency
    assert _normalize_currency("¥") == "JPY"
    assert _normalize_currency("฿") == "THB"
    assert _normalize_currency("£") == "GBP"
    assert _normalize_currency("إ.د") == "AED"
    assert _normalize_currency("€") == "EUR"
    assert _normalize_currency("usd") == "USD"  # .upper() fallback


def test_isracard_japan_air_uses_charge_eur_not_yen_sticker():
    """Foreign settlement: prefer charge € over merchant-local ¥ sticker."""
    from argosy.services.expense_ingest.parsers.isracard import parse

    # Prefer the uploaded July-2026 statement that carries the two Japan Air rows.
    uploads = Path(__file__).resolve().parents[1] / "uploads" / "ariel"
    sample = next(
        (p for p in uploads.rglob("*1266_07_2026.xlsx")),
        None,
    )
    if sample is None and _SAMPLES:
        root = Path(_SAMPLES)
        sample = next(iter(root.glob("**/1266/*07*2026*.xlsx")), None)
    if sample is None:
        pytest.skip("1266_07_2026 sample not present")

    result = parse(sample)
    japan = [t for t in result.transactions if "JAPAN AIR" in t.merchant_raw.upper()]
    assert len(japan) == 2, [t.merchant_raw for t in japan]
    amounts = sorted(t.amount_orig for t in japan)
    assert amounts == pytest.approx([244.72, 705.36])
    assert all(t.currency_orig == "EUR" for t in japan)
    assert all(t.amount_nis is None for t in japan)
    # No symbol currencies leak into currency_orig.
    assert all(
        t.currency_orig is None
        or (t.currency_orig.isascii() and t.currency_orig.isalpha())
        for t in result.transactions
    )
    # raw_row keeps the merchant-local sticker (normalized to JPY) for reference.
    assert {t.raw_row["tx_ccy"] for t in japan} == {"JPY"}
    assert {t.raw_row["charge_ccy"] for t in japan} == {"EUR"}


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
    assert refund.amount_nis == 268.43  # always positive


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


# ---------------------------------------------------------------------------
# Leumi USD parser tests — live-fixture gated (no synthetic fixture yet;
# the HTML wrapper is large enough that hand-rolling one isn't worthwhile).
# Skip if ARGOSY_EXPENSE_SAMPLES_ROOT is unset.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _SAMPLES, reason="ARGOSY_EXPENSE_SAMPLES_ROOT unset")
def test_leumi_usd_parser_live_fixture():
    """Run the parser on the live USD fixture and assert the basic shape:
    every row carries currency_orig='USD', amount_nis is None, source_hint
    points at the USD account number 44745200.
    """
    from argosy.services.expense_ingest.parsers.leumi_usd import parse
    samples = Path(_SAMPLES)
    candidates = sorted(samples.glob("**/Leumi/usd.xls"))
    if not candidates:
        pytest.skip("no Leumi USD samples present")
    result = parse(candidates[0])
    assert len(result.transactions) > 0
    assert all(t.currency_orig == "USD" for t in result.transactions)
    assert all(t.amount_nis is None for t in result.transactions)
    assert all(t.amount_orig is not None for t in result.transactions)
    assert result.source_hint is not None
    assert result.source_hint.kind == "bank"
    assert result.source_hint.issuer == "leumi"
    assert result.source_hint.external_id == "44745200"
    assert result.source_hint.display_name == "Leumi USD account"


@pytest.mark.skipif(not _SAMPLES, reason="ARGOSY_EXPENSE_SAMPLES_ROOT unset")
def test_leumi_usd_parser_extended_description_in_raw_row():
    """Verify the raw_row dict carries the semantic Hebrew column keys."""
    from argosy.services.expense_ingest.parsers.leumi_usd import parse
    samples = Path(_SAMPLES)
    candidates = sorted(samples.glob("**/Leumi/usd.xls"))
    if not candidates:
        pytest.skip("no Leumi USD samples present")
    result = parse(candidates[0])
    sample = result.transactions[0].raw_row
    # All seven expected keys present (see leumi_usd.parse for the schema).
    expected_keys = {
        "date", "description", "extended_description", "reference",
        "debit_usd", "credit_usd", "balance_usd",
    }
    assert expected_keys.issubset(sample.keys())


# ---------------------------------------------------------------------------
# Custody-view rejection: the securities sub-account export shares the cash
# ledger's header + top account number but lists value-date clearing pairs.
# Ingesting it double-counts booked trades (live incident 2026-07-13).
# ---------------------------------------------------------------------------

_CUSTODY_HTML = """<HTML dir="RTL"><head><META http-equiv="Content-Type" content="text/html; charset=utf-8"></head>
<body>
<span>בנק לאומי - תנועות בחשבון מט"ח</span>
<span>חשבון:</span><span>‏ל"וחב םירחסנ ע"ינ‏ ‎447452/00 968‎ דולר ארה"ב</span>
<table>
<tr><td>תאריך</td><td>תיאור</td><td>תאור מורחב</td><td>אסמכתא</td><td>חובה</td><td>זכות</td><td>יתרה</td></tr>
<tr><td>16/06/26</td><td>נ"ע-פעולה</td><td></td><td>813322</td><td></td><td>5,499.90</td><td>-48,418.00</td></tr>
<tr><td>16/06/26</td><td>נ"ע בבורסה (ת.ערך: 17/06/26)</td><td></td><td>813355</td><td>5,499.90</td><td></td><td>-53,917.90</td></tr>
</table>
</body></HTML>"""

_CASHLIKE_HTML = _CUSTODY_HTML.replace(
    '‏ל"וחב םירחסנ ע"ינ‏ ‎447452/00 968‎', '‏פמ"ח יחידים דו...‏ ‎447452/00 094‎'
)


def test_leumi_usd_rejects_custody_view_by_header(tmp_path):
    from argosy.services.expense_ingest.parsers.leumi_usd import (
        LeumiCustodyViewError, is_custody_view, parse,
    )
    p = tmp_path / "custody.xls"
    p.write_text(_CUSTODY_HTML, encoding="utf-8")
    assert is_custody_view(p) is True
    with pytest.raises(LeumiCustodyViewError):
        parse(p)


def test_leumi_usd_cash_header_is_not_custody(tmp_path):
    """The פמ"ח cash-ledger header must NOT trip the custody sniff.
    (Row descriptions are deliberately NOT a discriminator — the real
    cash ledger also carries 'נ"ע-פעולה' settlement rows.)"""
    from argosy.services.expense_ingest.parsers.leumi_usd import is_custody_view

    p = tmp_path / "cashlike.xls"
    p.write_text(_CASHLIKE_HTML, encoding="utf-8")
    assert is_custody_view(p) is False


def test_leumi_usd_format_audit_skips_custody_view(tmp_path):
    """The samples-walking audit lists a custody export as SKIPPED-by-design,
    not FAIL — the tripwire stays green when the file is present on disk."""
    from argosy.services.expense_ingest.format_audit import audit_file
    p = tmp_path / "leumi_2099_custody_usd.xls"
    p.write_text(_CUSTODY_HTML, encoding="utf-8")
    row = audit_file(tmp_path, p)
    assert row.status == "SKIPPED"
    assert "custody" in row.skip_reason
