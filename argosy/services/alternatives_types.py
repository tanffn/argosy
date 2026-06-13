"""Typed objects for the team-sourced, verified Alternatives sleeve.

These are the structured objects the alternatives subflow produces and the
canonical engine consumes. Per Argosy doctrine the sleeve's SIZE and INSTRUMENTS
are both team-derived — ``target_pct=0`` with no instruments is a fully valid
team outcome (no estate-clean verified candidate, thin evidence, or the sleeve's
risk forcing too much FI for too little benefit). Validation enforces the
0%/instrument coherence so a malformed decision can never reach the engine.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class VerificationEvidence(BaseModel):
    """The deterministic facts gathered while verifying one instrument."""

    isin_checksum_ok: bool
    isin_prefix: str | None = None  # first 2 chars of the ISIN (issuing country)
    domicile_coherent: bool  # claimed domicile not contradicted by the ISIN prefix
    registry_hit: bool  # found in the verified-facts registry
    tradeable: bool | None = None  # optional yfinance cross-check; None = not checked
    source_url: str | None = None  # authoritative factsheet / ISIN-registry URL


class VerificationResult(BaseModel):
    """The verdict for one proposed instrument. ``verified`` is True ONLY when the
    instrument is a real, estate-clean, tradeable security — never on agent claim
    alone."""

    symbol: str
    verified: bool
    severity: Literal["GREEN", "YELLOW", "RED"]
    reason: str
    evidence: VerificationEvidence


class VerifiedAlternativesCandidate(BaseModel):
    """A proposed instrument that PASSED deterministic verification + the estate
    gate, and so is admissible for the canonical sleeve."""

    symbol: str
    name: str
    asset_class: str
    domicile: str
    isin: str
    weight_within_sleeve_pct: float
    conviction: Literal["HIGH", "MEDIUM", "LOW"]
    thesis_md: str
    verification: VerificationResult


class AlternativesSleeveDecision(BaseModel):
    """The team's final, gated decision for the Alternatives sleeve.

    ``target_pct`` may be 0 (a legitimate team answer). Invariant: a 0% sleeve
    carries no instruments; a non-zero sleeve must carry instruments.
    """

    target_pct: float = Field(ge=0.0, description="Final sleeve % of book; 0 is valid.")
    sleeve_sigma: float = Field(ge=0.0, description="Computed from verified instruments.")
    instruments: list[VerifiedAlternativesCandidate] = Field(default_factory=list)
    decision: Literal["approve", "cut", "0_percent", "insufficient_data"]
    rationale_md: str
    review_summary_md: str = ""
    violations: list[str] = Field(default_factory=list)

    def model_post_init(self, __context: object) -> None:
        if self.target_pct == 0 and self.instruments:
            raise ValueError("0% sleeve must carry no instruments")
        if self.target_pct > 0 and not self.instruments:
            raise ValueError("non-zero sleeve must carry instruments")


__all__ = [
    "VerificationEvidence",
    "VerificationResult",
    "VerifiedAlternativesCandidate",
    "AlternativesSleeveDecision",
]
