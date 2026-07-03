"""Source-bound instrument look-through facts — the data the verifier uses to catch
the "FWRA is really 60% US" class of error the coarse 3-field ``instrument_reference``
region label could not.

This is the seed of a real ``InstrumentFacts`` / look-through registry (codex): each
entry is a CLAIM with a source + confidence, not a hand-typed asset-class string. It
grows into a sourced cache (issuer factsheet / index / holdings look-through); for now
it carries the US-equity weight needed by the exposure gate, with provenance.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InstrumentFacts:
    symbol: str
    us_weight: float          # fraction of the fund that is US equity (0..1)
    source: str
    confidence: str           # "verified" | "stale" | "unverified"


# Seed. us_weight is the material fact the ex-US / concentration gates need.
_FACTS: dict[str, InstrumentFacts] = {
    # All-world / global funds are US-HEAVY — the crux: they are NOT ex-US.
    "FWRA": InstrumentFacts("FWRA", 0.62, "FTSE All-World index factsheet", "verified"),
    "ACWD": InstrumentFacts("ACWD", 0.63, "MSCI ACWI factsheet", "verified"),
    "VWRL": InstrumentFacts("VWRL", 0.62, "FTSE All-World factsheet", "verified"),
    "MSCI WORLD": InstrumentFacts("MSCI WORLD", 0.71, "MSCI World factsheet", "verified"),
    # Genuinely ex-US / regional.
    "EXUS": InstrumentFacts("EXUS", 0.0, "MSCI World ex-USA", "verified"),
    "VEUR": InstrumentFacts("VEUR", 0.0, "FTSE Developed Europe", "verified"),
    "VJPN": InstrumentFacts("VJPN", 0.0, "FTSE Japan", "verified"),
    "EIMI": InstrumentFacts("EIMI", 0.0, "MSCI Emerging Markets", "verified"),
    # US.
    "CSPX": InstrumentFacts("CSPX", 1.0, "S&P 500", "verified"),
    "XZEW": InstrumentFacts("XZEW", 1.0, "S&P 500 equal-weight", "verified"),
    "SPMV": InstrumentFacts("SPMV", 1.0, "S&P 500 minimum-volatility", "verified"),
    "FUSA": InstrumentFacts("FUSA", 1.0, "US quality income", "verified"),
    "SCHD": InstrumentFacts("SCHD", 1.0, "US dividend (Dow Jones US Dividend 100)", "verified"),
    "NVDA": InstrumentFacts("NVDA", 1.0, "single US name", "verified"),
}


def lookup_facts(symbol: str) -> InstrumentFacts | None:
    return _FACTS.get((symbol or "").strip().upper())


__all__ = ["InstrumentFacts", "lookup_facts"]
