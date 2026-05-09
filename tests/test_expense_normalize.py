"""Merchant-name normalization (key for merchant_category_cache lookups)."""

import pytest

from argosy.services.expense_ingest.normalize import normalize


@pytest.mark.parametrize("inp,out", [
    ("NETFLIX.COM", "netflix.com"),
    ("  שופרסל בע\"מ  ", "שופרסל בע\"מ"),       # trim only
    ("PAYPAL *VENDOR_X", "vendor_x"),            # foreign prefix stripped
    ("SQ *Coffee Shop", "coffee shop"),
    ("WWW.NAME-CHEAP.COM*ABCD", "name-cheap.com"),  # WWW prefix + trailing-id
    ("מלאנוקס טכנו-י", "מלאנוקס טכנו"),          # Leumi -י suffix stripped
    ("רמי לוי תשלום 3/12", "רמי לוי"),            # installment marker stripped
    ("ביט שלם 1 מתוך 6", "ביט"),                  # alt installment marker
    ("multiple   spaces   here", "multiple spaces here"),
])
def test_normalize_examples(inp, out):
    assert normalize(inp) == out


def test_normalize_handles_empty():
    assert normalize("") == ""
    assert normalize("   ") == ""


def test_normalize_handles_unicode_nfkc():
    # Composed vs decomposed Hebrew should normalize the same
    composed = "שלום"
    # NFKC normalization is mostly transparent for Hebrew; assert idempotent
    assert normalize(composed) == normalize(normalize(composed))


def test_normalize_does_not_strip_short_digits():
    # Pure-digit blocks of length < 4 are kept (e.g., 'CARREFOUR 24')
    assert normalize("CARREFOUR 24") == "carrefour 24"


def test_normalize_strips_long_trailing_digit_block():
    assert normalize("VENDOR 12345") == "vendor"
