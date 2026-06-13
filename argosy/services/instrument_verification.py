"""Deterministic instrument verification for the Alternatives sleeve.

The agent team PROPOSES instruments; this service establishes, with NO trust in
the agent's claims, that each pick is a REAL, tradeable, non-US-domiciled
security before it can become a holding. The deterministic core (ISIN ISO-6166
checksum + country-prefix<->domicile coherence + a verified-facts registry)
needs no network; an OPTIONAL yfinance cross-check confirms tradeability where
coverage exists.

Doctrine: the registry verifies FACTS about whatever the team picks; it is NOT
an allow-list that constrains the candidate universe. An instrument absent from
the registry is not forbidden -- it is UNVERIFIED, and an unverified instrument
can never become a holding until its facts are confirmed against an authoritative
source. Frozen registry entries are seeds for verification, not authority over
what the team may propose.
"""
from __future__ import annotations

import string
from functools import lru_cache
from pathlib import Path

import yaml

# Plausible ISIN issuing-country prefixes for this book. US is allowed at the
# checksum layer (a US ISIN is structurally valid); the estate/coherence layer is
# what rejects US-situs. "XS" is the Euroclear/Clearstream international prefix,
# common for European ETPs.
_ISO_COUNTRY_PREFIXES = frozenset(
    {"IE", "LU", "DE", "FR", "GB", "JE", "GG", "CH", "NL", "US", "CA", "IL", "XS"}
)

_ALPHA = string.ascii_uppercase


def isin_country_prefix(isin: str | None) -> str | None:
    """The two-letter issuing-country prefix of a 12-char ISIN, else None."""
    if not isin or len(isin) != 12:
        return None
    p = isin[:2].upper()
    return p if p.isalpha() else None


def isin_is_valid(isin: str | None) -> bool:
    """True iff ``isin`` is a structurally valid ISO 6166 identifier with a correct
    check digit and a plausible country prefix.

    Algorithm (ISO 6166): expand each letter to two digits (A=10 ... Z=35), then
    apply the Luhn mod-10 check over the resulting digit string.
    """
    if not isin or len(isin) != 12:
        return False
    s = isin.upper()
    if not (s[:2].isalpha() and s[2:11].isalnum() and s[11].isdigit()):
        return False
    if s[:2] not in _ISO_COUNTRY_PREFIXES:
        return False

    # Expand letters -> digits.
    digits: list[int] = []
    for ch in s:
        if ch.isdigit():
            digits.append(int(ch))
        elif ch in _ALPHA:
            v = 10 + _ALPHA.index(ch)
            digits.append(v // 10)
            digits.append(v % 10)
        else:
            return False

    # Luhn from the rightmost digit: double every second digit.
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@lru_cache(maxsize=1)
def load_registry() -> dict[str, dict]:
    """Load the verified-facts registry, keyed by upper-cased symbol.

    Returns an empty dict if the file is missing -- an empty registry means
    every proposed instrument is unverified (and therefore rejected), which is
    the correct fail-safe.
    """
    path = Path(__file__).resolve().parent.parent / "data" / "verified_instruments.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(k).upper(): v for k, v in data.items()}


def registry_lookup(symbol: str, registry: dict[str, dict]) -> dict | None:
    """Return the verified-facts row for ``symbol`` (case-insensitive), else None."""
    return registry.get(symbol.upper())


def verify_instrument(
    *, symbol: str, claimed_domicile: str | None, claimed_isin: str | None
):
    """Verify one proposed instrument deterministically. Returns a
    :class:`~argosy.services.alternatives_types.VerificationResult`.

    Decision table (no trust in the agent's claims; nothing unverified is ever
    ``verified=True``):

    - GREEN (verified): registry-confirmed AND checksum-valid AND domicile-coherent
      AND non-US domicile.
    - RED (reject): a US ISIN prefix, a US domicile claim, or a structurally
      invalid ISIN -- a fabricated/US-situs pick that must never become a holding.
    - YELLOW (reject): unknown / unstamped -- not in the registry and not provably
      bad; still cannot become a holding until its facts are confirmed.

    The registry is authoritative when present: a hit's ISIN/domicile override the
    agent's claim so a wrong claim about a known instrument cannot mislead.
    """
    from argosy.services.alternatives_types import (
        VerificationEvidence,
        VerificationResult,
    )

    registry = load_registry()
    hit = registry_lookup(symbol, registry)
    isin = (hit or {}).get("isin", claimed_isin)
    domicile = (hit or {}).get("domicile", claimed_domicile)

    checksum_ok = isin_is_valid(isin)
    prefix = isin_country_prefix(isin)
    is_us_domicile = (domicile or "").upper() == "US"
    # Coherence: a US ISIN prefix contradicts any non-US domicile claim; otherwise
    # the prefix must be a real (non-empty) country code.
    coherent = bool(prefix) and not (prefix == "US" and not is_us_domicile)

    evidence = VerificationEvidence(
        isin_checksum_ok=checksum_ok,
        isin_prefix=prefix,
        domicile_coherent=coherent,
        registry_hit=hit is not None,
        tradeable=None,
        source_url=(hit or {}).get("source_url"),
    )

    if hit is not None and checksum_ok and coherent and not is_us_domicile:
        return VerificationResult(
            symbol=symbol,
            verified=True,
            severity="GREEN",
            reason="registry-confirmed; checksum + domicile coherence pass",
            evidence=evidence,
        )
    if prefix == "US" or is_us_domicile or (isin and not checksum_ok):
        return VerificationResult(
            symbol=symbol,
            verified=False,
            severity="RED",
            reason="US-situs (US prefix/domicile) or failed ISIN checksum",
            evidence=evidence,
        )
    return VerificationResult(
        symbol=symbol,
        verified=False,
        severity="YELLOW",
        reason="unverified: not in registry / unstamped — cannot become a holding",
        evidence=evidence,
    )


__all__ = [
    "isin_is_valid",
    "isin_country_prefix",
    "load_registry",
    "registry_lookup",
    "verify_instrument",
]
