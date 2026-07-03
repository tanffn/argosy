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
    amount_usd: float = Field(ge=0.0)  # a buy can never be negative (schema defense)
    sleeve: str = ""
    justification: str = ""
    # The agent's claim about the instrument's US-equity weight (0..1), checked
    # against the sourced InstrumentFacts registry by the verifier. Required at the
    # gate — a None here makes the look-through cross-check un-skippable.
    claimed_us_weight: float | None = None


class Sell(BaseModel):
    symbol: str
    amount_usd: float = Field(ge=0.0)
    reason: str = ""


class AllocationProposal(BaseModel):
    """The fleet's authored move for a deploy request."""

    # Non-negativity is a schema invariant AND re-checked in the verifier (the
    # authoritative money gate): a negative reserve must never be able to balance an
    # over-deploy through the pure-equality conservation checks.
    #
    # There is deliberately NO tax-reserve field. Capital-gains tax on a sale is paid
    # from that sale's own proceeds when it is realized — you do not pre-fund a future
    # sale's tax out of unrelated deployment cash. The deployable amount is treated as
    # already net-of-tax.
    cash_to_deploy: float = Field(ge=0.0)
    cash_to_reserve: float = Field(default=0.0, ge=0.0)
    buys: list[Buy] = Field(default_factory=list)
    sells: list[Sell] = Field(default_factory=list)
    holds: list[str] = Field(default_factory=list)
    rationale: str = ""


__all__ = ["AllocationProposal", "Buy", "Sell"]
