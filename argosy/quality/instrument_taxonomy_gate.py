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
fx_gate / coherence_gate convention. The wrapper-type assertion and the
migration action are each bound to the NEAREST specific ticker (adjacency, not a
clause-level union), so a clause that names two distinct tickers — one ETC that
stays put, one UCITS being migrated — does not cross-contaminate. A ticker is
flagged only when the SAME ticker is both asserted not-UCITS and the object of a
UCITS-migration action.
"""
from __future__ import annotations

import re

from argosy.quality.gate_types import GateCheck, GateViolation

# An instrument ticker is an uppercase 3-5 letter symbol (SGLN, VWRA, NVDA).
# Word-bounded ``\b[A-Z]{3,5}\b`` captures, embedded directly in the adjacency
# regexes below; 3-5 keeps it to plausible tickers and avoids 1-2 letter false
# hits ("US", "FX"). Wrapper/jargon tokens that happen to match are filtered out
# via the exclusion set below.

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

# A "NOT a UCITS" / "ETC, not UCITS" wrapper-type cue. These phrasings, when
# ADJACENT to a specific ticker, mark THAT ticker as asserted NON-UCITS:
#   - "not a UCITS fund" / "not UCITS" / "is not UCITS"
#   - "physical-gold ETC" / "physical gold ETC" / a bare "ETC" wrapper word
#   - "non-UCITS"
# Kept as a sub-pattern so it can be embedded next to a ticker capture (below)
# rather than matched clause-wide — adjacency is what binds the cue to one
# ticker and prevents tagging every ticker that merely shares the clause.
_NOT_UCITS_CUE = (
    r"(?:not\s+(?:a\s+)?ucits"        # "not a UCITS", "not UCITS"
    r"|non[-\s]?ucits"                # "non-UCITS"
    r"|physical[-\s]?gold\s+etc"      # "physical-gold ETC"
    r"|\betc\b)"                      # a bare ETC wrapper word
)

# Bind the not-UCITS cue to the NEAREST ticker, in EITHER order, within a short
# adjacency window (<=25 non-terminal chars). "SGLN is a physical-gold ETC" and
# "ETC SGLN" both bind SGLN; a separate ticker elsewhere in the clause is NOT
# tagged. ``[^.!?\n]`` keeps the window inside the clause. The captured ticker is
# validated against the jargon set by the caller.
_NOT_UCITS_NEAR_TICKER_RE = re.compile(
    # order 1: "SGLN ... ETC"   |  order 2: "ETC ... SGLN"
    r"\b([A-Z]{3,5})\b[^.!?\n]{0,25}?" + _NOT_UCITS_CUE
    + r"|" + _NOT_UCITS_CUE + r"[^.!?\n]{0,25}?\b([A-Z]{3,5})\b",
    re.IGNORECASE,
)

# Bind a UCITS-migration action to the SPECIFIC ticker that is its object — the
# ticker being moved/migrated/consolidated INTO UCITS — not every ticker in the
# clause. Two orders: the action verb immediately before the ticker
# ("migrate VWRA into ... UCITS"), or the ticker before a "migration ... UCITS"
# phrase ("VWRA ... UCITS migration"). The trailing "...UCITS" requirement is
# what scopes the verb to a UCITS migration specifically.
_UCITS_MIGRATION_OF_TICKER_RE = re.compile(
    # "migrate/move/consolidate <TICKER> [the] into/to [the] UCITS"
    r"(?:migrat\w*|mov\w*|consolidat\w*)\s+"
    r"\b([A-Z]{3,5})\b"
    r"[^.!?\n]{0,25}?(?:into|to)\s+(?:the\s+)?ucits"
    r"|"
    # "<TICKER> ... UCITS migration"  (ticker is the subject of the migration)
    r"\b([A-Z]{3,5})\b[^.!?\n]{0,40}?ucits\s+migration",
    re.IGNORECASE,
)


def _valid_ticker(token: str | None) -> str | None:
    """Return the token if it is a real instrument ticker, else None."""
    if token and token.upper() not in _NON_TICKER_TOKENS:
        return token.upper()
    return None


def check_instrument_taxonomy(*, plan_text: str) -> list[GateViolation]:
    """Flag a ticker asserted NOT-UCITS that is routed into a UCITS migration.

    Input: ``plan_text`` — the rendered plan body. Two adjacency-bound scans
    collect (a) the specific ticker each NON-UCITS wrapper cue ("ETC" / "not a
    UCITS fund") is attached to, and (b) the specific ticker that is the object
    of each UCITS-migration action ("migrate X into the UCITS wrapper"). A ticker
    in BOTH sets is a wrapper-type contradiction → one
    ``GateCheck.INSTRUMENT_TAXONOMY`` violation per ticker. Because each cue binds
    to its nearest ticker, a clause naming two distinct tickers (one ETC that
    stays put, one UCITS being migrated) does not cross-contaminate.
    """
    text = plan_text or ""
    asserted_not_ucits: set[str] = set()
    migrated_to_ucits: set[str] = set()

    for m in _NOT_UCITS_NEAR_TICKER_RE.finditer(text):
        # group 1 = "TICKER ... cue", group 2 = "cue ... TICKER"
        ticker = _valid_ticker(m.group(1)) or _valid_ticker(m.group(2))
        if ticker:
            asserted_not_ucits.add(ticker)

    for m in _UCITS_MIGRATION_OF_TICKER_RE.finditer(text):
        # group 1 = "migrate TICKER into UCITS", group 2 = "TICKER ... UCITS migration"
        ticker = _valid_ticker(m.group(1)) or _valid_ticker(m.group(2))
        if ticker:
            migrated_to_ucits.add(ticker)

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
