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

_ALPHA = string.ascii_uppercase

# Valid ISIN country prefixes = the official ISO 3166-1 alpha-2 set plus the
# special international prefixes NNAs assign (XS Euroclear/Clearstream, EU, QS/QT
# substitute codes). A prefix outside this set (e.g. the reserved "ZZ") fails the
# structural check even if its Luhn digit happens to pass — closing the
# "fabricated Luhn-valid ISIN" hole. This is PURE structure, not estate policy.
_ISO_3166_ALPHA2 = frozenset(
    "AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ "
    "BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR "
    "CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR "
    "GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU "
    "ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ "
    "LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ "
    "MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF "
    "PG PH PK PL PM PN PR PS PT PW PY QA RO RS RU RW SA SB SC SD SE SG SH SI SJ "
    "SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT "
    "TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW".split()
)
_ISIN_SPECIAL_PREFIXES = frozenset({"XS", "EU", "QS", "QT", "QW", "QY", "QZ"})
_VALID_ISIN_PREFIXES = _ISO_3166_ALPHA2 | _ISIN_SPECIAL_PREFIXES

# Domicile normalization: the canonical non-US domicile set is exactly the
# AllocationInstrument.domicile Literal's non-US members, so anything that
# verifies GREEN is representable downstream (no verifier↔schema vocab drift).
# A synonym map catches country names + GB→UK so a hand-typed registry row is
# forgiven; XS is deliberately ABSENT (it is an ISIN prefix, never a domicile).
_CANONICAL_NON_US_DOMICILES = frozenset({"IE", "LU", "UK", "DE", "CH", "JE", "IL"})
_DOMICILE_SYNONYMS: dict[str, str] = {
    "US": "US", "USA": "US", "U.S.": "US", "U.S.A.": "US", "UNITED STATES": "US",
    "UNITED STATES OF AMERICA": "US",
    "IE": "IE", "IRELAND": "IE",
    "LU": "LU", "LUXEMBOURG": "LU",
    "UK": "UK", "GB": "UK", "GREAT BRITAIN": "UK", "UNITED KINGDOM": "UK",
    "DE": "DE", "GERMANY": "DE",
    "CH": "CH", "SWITZERLAND": "CH",
    "JE": "JE", "JERSEY": "JE",
    "IL": "IL", "ISRAEL": "IL",
}


def normalize_domicile(raw: str | None) -> str | None:
    """Map a domicile string (code or country name) to a canonical code, or None.

    Returns ``"US"`` for any US synonym; a canonical non-US code for a recognised
    non-US domicile; ``None`` otherwise (unrecognised → fail closed). The non-US
    codes returned are exactly those the downstream AllocationInstrument schema
    accepts, so a GREEN verification is always representable as a holding.
    """
    if not raw:
        return None
    return _DOMICILE_SYNONYMS.get(raw.strip().upper())


def isin_country_prefix(isin: str | None) -> str | None:
    """The two-letter issuing-country prefix of a 12-char ASCII ISIN, else None."""
    if not isin or len(isin) != 12 or not isin.isascii():
        return None
    p = isin[:2].upper()
    return p if p.isalpha() else None


def isin_is_valid(isin: str | None) -> bool:
    """True iff ``isin`` is a structurally valid ISO 6166 identifier with a correct
    check digit. This is PURE structural/checksum validity — it does NOT encode
    estate policy (US-situs rejection lives in :func:`verify_instrument` via the
    domicile/coherence layer), so any valid ISO country prefix is accepted here.

    Algorithm (ISO 6166): expand each letter to two digits (A=10 ... Z=35), then
    apply the Luhn mod-10 check over the resulting digit string.
    """
    # Strict ASCII: Python's isalpha()/isalnum()/isdigit()/upper() are Unicode-
    # aware, so lookalike chars (Arabic-Indic digits, eszett) would otherwise
    # score valid and could smuggle a fabricated identifier past the gate.
    if not isin or len(isin) != 12 or not isin.isascii():
        return False
    s = isin.upper()
    if len(s) != 12:  # defensive: ASCII upper() never changes length, but guard anyway
        return False
    if not (s[:2].isalpha() and s[2:11].isalnum() and s[11].isdigit()):
        return False
    # Prefix must be a real ISO 3166 alpha-2 code or an ISIN special prefix —
    # rejects fabricated Luhn-valid identifiers with reserved prefixes (e.g. ZZ).
    if s[:2] not in _VALID_ISIN_PREFIXES:
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
    """Return the verified-facts row for ``symbol`` (stripped, case-insensitive)."""
    if not symbol:
        return None
    return registry.get(symbol.strip().upper())


def verify_instrument(
    *,
    symbol: str,
    claimed_domicile: str | None,
    claimed_isin: str | None,
    registry: dict[str, dict] | None = None,
):
    """Verify one proposed instrument deterministically. Returns a
    :class:`~argosy.services.alternatives_types.VerificationResult`.

    Two regimes, both fail-closed (nothing unverified is ever ``verified=True``):

    **Registry hit (authoritative):** the agent's claims are IGNORED entirely. The
    row must be COMPLETE (isin + domicile + source_url) and its facts must pass:
    a valid ISIN checksum, a recognised non-US domicile (synonyms normalised, so
    "United States" cannot bypass), and ISIN-prefix↔domicile coherence. Any gap →
    RED (fail closed). On success, ``resolved_isin`` / ``resolved_domicile`` carry
    the authoritative facts the caller MUST bind the holding to.

    **No registry hit (unverified):** the pick cannot become a holding. It is RED
    if its claimed facts are provably bad/US-situs (US prefix/domicile or failed
    checksum), else YELLOW (unknown). Either way ``verified=False``.

    ``registry`` may be injected for testing; defaults to the on-disk registry.
    """
    from argosy.services.alternatives_types import (
        VerificationEvidence,
        VerificationResult,
    )

    reg = registry if registry is not None else load_registry()
    hit = registry_lookup(symbol, reg)

    if hit is not None:
        # Authoritative regime — trust ONLY the registry row, never the claim.
        r_isin = hit.get("isin")
        r_domicile = normalize_domicile(hit.get("domicile"))
        r_source = hit.get("source_url")
        checksum_ok = isin_is_valid(r_isin)
        prefix = isin_country_prefix(r_isin)
        # coherence: US ISIN prefix may only pair with a US domicile (which we
        # reject anyway); a non-US prefix is coherent with a non-US domicile.
        coherent = bool(prefix) and not (prefix == "US" and r_domicile != "US")
        # A complete row must carry the full audit identity: a recognised
        # domicile, an http(s) source, a verification date, and an exchange. A
        # thin row (real ISIN mapped to an unproven ticker) must NOT verify.
        complete = (
            bool(r_isin)
            and r_domicile is not None
            and isinstance(r_source, str)
            and r_source.lower().startswith(("http://", "https://"))
            and bool(hit.get("verified_on"))
            and bool(hit.get("exchange"))
        )
        evidence = VerificationEvidence(
            isin_checksum_ok=checksum_ok,
            isin_prefix=prefix,
            domicile_coherent=coherent,
            registry_hit=True,
            tradeable=None,
            source_url=r_source,
        )
        if complete and checksum_ok and coherent and r_domicile not in (None, "US"):
            return VerificationResult(
                symbol=symbol, verified=True, severity="GREEN",
                reason="registry-confirmed; checksum + domicile coherence pass",
                evidence=evidence, resolved_isin=r_isin, resolved_domicile=r_domicile,
            )
        return VerificationResult(
            symbol=symbol, verified=False, severity="RED",
            reason="registry row incomplete/US/incoherent — fail closed",
            evidence=evidence,
        )

    # Unverified regime — judge the claim only to choose RED vs YELLOW.
    checksum_ok = isin_is_valid(claimed_isin)
    prefix = isin_country_prefix(claimed_isin)
    claimed_dom = normalize_domicile(claimed_domicile)
    is_us = prefix == "US" or claimed_dom == "US"
    evidence = VerificationEvidence(
        isin_checksum_ok=checksum_ok,
        isin_prefix=prefix,
        domicile_coherent=bool(prefix) and not (prefix == "US" and claimed_dom != "US"),
        registry_hit=False,
        tradeable=None,
        source_url=None,
    )
    if is_us or (claimed_isin and not checksum_ok):
        return VerificationResult(
            symbol=symbol, verified=False, severity="RED",
            reason="US-situs (US prefix/domicile) or failed ISIN checksum",
            evidence=evidence,
        )
    return VerificationResult(
        symbol=symbol, verified=False, severity="YELLOW",
        reason="unverified: not in registry / unstamped — cannot become a holding",
        evidence=evidence,
    )


__all__ = [
    "isin_is_valid",
    "isin_country_prefix",
    "normalize_domicile",
    "load_registry",
    "registry_lookup",
    "verify_instrument",
]
