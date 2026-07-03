"""The structured allocation the FLEET authors — the object determinism verifies.

The author (an LLM agent) reasons holistically over the decision packet and emits
this; it is never produced by deterministic code. Every material instrument claim
the agent relies on (e.g. "FWRA is ex-US") is carried as a field so the verifier can
check it against sourced facts rather than trusting prose.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class Buy(BaseModel):
    symbol: str
    amount_usd: float
    sleeve: str = ""
    justification: str = ""
    # The agent's claim about the instrument's US-equity weight (0..1), checked
    # against the sourced InstrumentFacts registry by the verifier.
    claimed_us_weight: float | None = None


class Sell(BaseModel):
    symbol: str
    amount_usd: float
    reason: str = ""


class AllocationProposal(BaseModel):
    """The fleet's authored move for a deploy request."""

    cash_to_deploy: float
    cash_to_reserve: float = 0.0
    cash_reserved_for_tax: float = 0.0
    buys: list[Buy] = Field(default_factory=list)
    sells: list[Sell] = Field(default_factory=list)
    holds: list[str] = Field(default_factory=list)
    rationale: str = ""


__all__ = ["AllocationProposal", "Buy", "Sell"]
