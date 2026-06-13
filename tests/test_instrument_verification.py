"""Tests for deterministic instrument verification (ISIN checksum, coherence,
registry lookup, composed verdict)."""
from __future__ import annotations

from argosy.services.instrument_verification import (
    isin_country_prefix,
    isin_is_valid,
    load_registry,
    registry_lookup,
    verify_instrument,
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


def test_non_ascii_lookalike_isins_rejected():
    # Codex finding: Unicode-aware isdigit()/isalpha()/upper() let lookalike chars
    # score valid. A strict ASCII guard must reject them.
    assert isin_is_valid("IE00B579F32٥") is False  # Arabic-Indic digit 5 (check pos)
    assert isin_is_valid("IE00B579F3٢5") is False  # Arabic-Indic digit 2 in body
    assert isin_is_valid("IEß000000005") is False  # German eszett (upper -> SS)


def test_country_prefix_rejects_non_ascii():
    assert isin_country_prefix("ΙE00B579F325") is None  # Greek capital Iota


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


def test_known_clean_instrument_verifies_green():
    r = verify_instrument(symbol="SGLD", claimed_domicile="IE", claimed_isin="IE00B579F325")
    assert r.verified
    assert r.severity == "GREEN"
    assert r.evidence.registry_hit
    assert r.evidence.isin_checksum_ok
    assert r.evidence.domicile_coherent


def test_us_prefix_isin_with_nonus_claim_is_red():
    # Claims IE domicile but supplies a real US ISIN (NVDA) -> incoherent -> never hold.
    r = verify_instrument(symbol="NOTREAL", claimed_domicile="IE", claimed_isin="US67066G1040")
    assert not r.verified
    assert r.severity == "RED"
    assert not r.evidence.domicile_coherent


def test_failed_checksum_is_red():
    r = verify_instrument(symbol="BADISIN", claimed_domicile="IE", claimed_isin="IE00B579F320")
    assert not r.verified
    assert r.severity == "RED"
    assert not r.evidence.isin_checksum_ok


def test_unknown_unverifiable_instrument_is_yellow_not_held():
    r = verify_instrument(symbol="MADEUP", claimed_domicile="IE", claimed_isin=None)
    assert not r.verified
    assert r.severity == "YELLOW"


def test_us_domicile_claim_never_verifies():
    # Even with a structurally valid US ISIN, a US-domiciled pick is never held.
    r = verify_instrument(symbol="USFUND", claimed_domicile="US", claimed_isin="US67066G1040")
    assert not r.verified
    assert r.severity == "RED"
