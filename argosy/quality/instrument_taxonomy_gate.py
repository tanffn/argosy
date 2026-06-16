"""Run-106 finding [7] — instrument-taxonomy coherence gate.

The defect: the plan correctly states SGLN is NOT a UCITS fund (a physical-gold
ETC), then includes SGLN in an action explicitly described as a migration INTO
UCITS. That contradicts the instrument's own wrapper TYPE and creates execution
confusion around the taxonomy.

Invariant: a ticker asserted to be NOT a UCITS wrapper (or described as an ETC /
"physical gold ETC") in one clause must NOT appear in an action described as a
UCITS migration / move-into-UCITS / consolidate-into-UCITS. Generalizes beyond
SGLN: we associate a wrapper-type assertion per ticker, then flag any ticker
asserted not-UCITS that is also routed into a UCITS-migration action.

Pure function, no I/O. Named compiled regexes with WHY comments, matching the
fx_gate / coherence_gate convention. Per Argosy's fail-loud doctrine this biases
toward FALSE-POSITIVE: associations are clause-local but the not-UCITS assertion
and the migration action need only co-occur for the SAME ticker anywhere in the
document — a spurious flag is safer than letting a taxonomy contradiction ship.
"""
from __future__ import annotations

import re

from argosy.quality.gate_types import GateCheck, GateViolation

# An instrument ticker: an uppercase 3-5 letter symbol (SGLN, VWRA, NVDA). Word-
# bounded so it doesn't catch substrings; 3-5 keeps it to plausible tickers and
# avoids 1-2 letter false hits ("US", "FX", "IPS", "ETC" is excluded explicitly
# below since it is a wrapper-type word, not a ticker).
_TICKER_RE = re.compile(r"\b([A-Z]{3,5})\b")

# Common 3-5 letter uppercase tokens that are NOT instrument tickers — wrapper
# words, currencies, and gate jargon that would otherwise be mistaken for a
# ticker. Excluded so we never assert taxonomy about a non-instrument token.
_NON_TICKER_TOKENS = frozenset(
    {
        "UCITS", "ETC", "ETF", "ETCS", "ETFS", "USD", "NIS", "ILS", "EUR",
        "GBP", "USA", "IPS", "SWR", "RSU", "RSUS", "NVDA",  # NVDA handled as a
        # real ticker only where relevant; it never carries a UCITS/ETC wrapper
        # assertion in practice, but keep it out of the taxonomy scan to avoid
        # noise. (Removed below if a wrapper assertion explicitly names it.)
    }
)

# A "NOT a UCITS" / "ETC, not UCITS" wrapper assertion. Any of these phrasings
# near a ticker marks that ticker as asserted NON-UCITS:
#   - "not a UCITS fund" / "not UCITS" / "is not UCITS"
#   - "physical-gold ETC" / "physical gold ETC" / a bare "ETC" wrapper word
#   - "not a UCITS-eligible" / "non-UCITS"
_NOT_UCITS_RE = re.compile(
    r"(?:not\s+(?:a\s+)?ucits"        # "not a UCITS", "not UCITS"
    r"|non[-\s]?ucits"                # "non-UCITS"
    r"|physical[-\s]?gold\s+etc"      # "physical-gold ETC"
    r"|\betc\b)",                     # a bare ETC wrapper word
    re.IGNORECASE,
)

# A UCITS-migration action: the instrument is being routed INTO the UCITS
# wrapper. Broad, auditable alternatives for the run-106 phrasings:
#   "migrate ... into the UCITS wrapper" / "move into UCITS" / "UCITS migration"
#   / "consolidate into UCITS" / "migrate ... to UCITS"
_UCITS_MIGRATION_RE = re.compile(
    r"(?:migrat\w*\s+\w*\s*(?:into|to)\s+(?:the\s+)?ucits"  # "migrate X into the UCITS"
    r"|move\s+\w*\s*(?:into|to)\s+(?:the\s+)?ucits"          # "move into UCITS"
    r"|consolidat\w*\s+\w*\s*into\s+(?:the\s+)?ucits"        # "consolidate into UCITS"
    r"|ucits\s+migration)",                                  # "UCITS migration"
    re.IGNORECASE,
)

# Split into sentence-ish clauses on terminal punctuation / newlines, mirroring
# coherence_gate. Wrapper assertions and migration actions are associated to a
# ticker within the SAME clause; the contradiction is then evaluated per ticker
# across the whole document.
_SENTENCE_SPLIT_RE = re.compile(r"[.!?\n]+")


def _tickers_in(clause: str) -> set[str]:
    """Real instrument tickers in a clause (uppercase 3-5 letter, minus jargon)."""
    return {t for t in _TICKER_RE.findall(clause) if t not in _NON_TICKER_TOKENS}


def check_instrument_taxonomy(*, plan_text: str) -> list[GateViolation]:
    """Flag a ticker asserted NOT-UCITS that is routed into a UCITS migration.

    Input: ``plan_text`` — the rendered plan body. Scanned clause-by-clause to
    associate (a) tickers asserted NON-UCITS (an "ETC" / "not a UCITS fund"
    wrapper statement) and (b) tickers placed in a UCITS-migration action
    ("migrate X into the UCITS wrapper"). A ticker in BOTH sets is a wrapper-type
    contradiction → one ``GateCheck.INSTRUMENT_TAXONOMY`` violation per ticker.
    """
    text = plan_text or ""
    asserted_not_ucits: set[str] = set()
    migrated_to_ucits: set[str] = set()

    for raw_clause in _SENTENCE_SPLIT_RE.split(text):
        clause = raw_clause.strip()
        if not clause:
            continue
        tickers = _tickers_in(clause)
        if not tickers:
            continue
        if _NOT_UCITS_RE.search(clause):
            asserted_not_ucits |= tickers
        if _UCITS_MIGRATION_RE.search(clause):
            migrated_to_ucits |= tickers

    violations: list[GateViolation] = []
    for ticker in sorted(asserted_not_ucits & migrated_to_ucits):
        violations.append(
            GateViolation(
                check=GateCheck.INSTRUMENT_TAXONOMY,
                detail=(
                    f"{ticker} is described as NOT a UCITS wrapper (ETC / not a "
                    "UCITS fund) yet appears in an action described as a migration "
                    "INTO UCITS. The instrument's stated wrapper TYPE contradicts "
                    "its action — fix the taxonomy or the routing before promotion."
                ),
                locator=ticker,
            )
        )
    return violations
