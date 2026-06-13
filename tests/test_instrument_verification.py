"""Tests for deterministic instrument verification (ISIN checksum, coherence,
registry lookup, composed verdict)."""
from __future__ import annotations

from argosy.services.instrument_verification import (
    isin_country_prefix,
    isin_is_valid,
    load_registry,
    registry_lookup,
)


def test_real_isin_passes_checksum():
    # NVDA — a well-known, genuinely valid ISIN (US prefix is fine for checksum).
    assert isin_is_valid("US67066G1040") is True


def test_corrupted_check_digit_fails():
    # Take the valid NVDA ISIN and change only the final check digit → invalid.
    assert isin_is_valid("US67066G1040") is True
    assert isin_is_valid("US67066G1041") is False


def test_structural_rejects():
    assert isin_is_valid("US67066G104") is False  # too short (11)
    assert isin_is_valid("US67066G10400") is False  # too long (13)
    assert isin_is_valid("ZZ67066G1040") is False  # implausible country code
    assert isin_is_valid(None) is False
    assert isin_is_valid("") is False


def test_country_prefix():
    assert isin_country_prefix("IE00B579F325") == "IE"
    assert isin_country_prefix("US67066G1040") == "US"
    assert isin_country_prefix("bad") is None
    assert isin_country_prefix(None) is None


def test_registry_lookup_known_instrument():
    reg = load_registry()
    hit = registry_lookup("SGLD", reg)
    assert hit is not None
    assert hit["domicile"] == "IE"
    assert hit["isin"] == "IE00B579F325"
    assert hit["source_url"]


def test_registry_lookup_is_case_insensitive():
    reg = load_registry()
    assert registry_lookup("sgld", reg) is not None


def test_registry_lookup_unknown_returns_none():
    assert registry_lookup("TOTALLY_MADE_UP", load_registry()) is None
