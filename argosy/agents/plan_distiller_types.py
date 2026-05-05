"""Pydantic types for the baseline plan distillate.

Per SDD §6.10 / spec §3: the distillate captures durable principles +
targets-as-stated; explicitly drops time-stamped numbers (current
portfolio %, FX rate, share counts, dated tranche schedules).

Each item carries a ``source_section`` pointer back to the heading in
the imported plan markdown for click-through provenance.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


# Allowed unit values for ``Target.unit``. Kept as a Literal for
# pydantic validation rather than a free-form string.
TargetUnit = Literal[
    "pct_of_portfolio",
    "pct_of_net_worth",
    "pct_of_liquid",
    "usd",
    "nis",
    "shares",
    "ratio",
    "years",
]


class Goal(BaseModel):
    """A durable goal extracted from the plan.

    Examples: retirement target year, target annual income, FI status,
    employment horizon. Goals are durable (years, not months) and rarely
    revised between syntheses.
    """

    label: str
    value: str
    rationale: str = ""
    source_section: str = ""
    user_edited: bool = False
    user_edit_note: str | None = None


class Principle(BaseModel):
    """An investment-philosophy principle from the plan.

    Examples: UCITS-first for estate safety, real-returns framework,
    NIS salary covers NIS expenses (natural hedge), concentration is
    the load-bearing risk.
    """

    label: str
    rationale: str = ""
    source_section: str = ""
    user_edited: bool = False
    user_edit_note: str | None = None


class DecisionRule(BaseModel):
    """A decision rule the user has committed to.

    Examples: bracket-aware RSU sales, gap-weighted deployment, no
    Defensive above cap, never panic-convert NIS<->USD.
    """

    label: str
    rule: str
    source_section: str = ""
    user_edited: bool = False
    user_edit_note: str | None = None


class Target(BaseModel):
    """A numeric target with explicit as-of stamping.

    Examples: NVDA -> 15%, defensive 5-8%, Core 20-25%, Growth 15-20%.
    The ``stated_at`` and ``revisit_after`` dates make the time-bound
    nature of the value explicit so consumers can age-down the
    recommendation as needed.
    """

    label: str
    value: float
    unit: TargetUnit
    stated_at: date
    revisit_after: date
    rationale: str = ""
    source_section: str = ""
    user_edited: bool = False
    user_edit_note: str | None = None


class Constraint(BaseModel):
    """An operational constraint the user has opted in to.

    Examples: no consolidate brokers, UCITS preferred, limited account
    capped at $1k, speculation max % cap.
    """

    label: str
    detail: str
    source_section: str = ""
    user_edited: bool = False
    user_edit_note: str | None = None


class PlanDistillate(BaseModel):
    """Compressed structured extract of a baseline plan.

    Target rendered size: 1500-2500 tokens. The only representation of
    the baseline that downstream synthesis ever consumes; the full
    ``raw_markdown`` is preserved for forensic / "show me the source"
    lookups but is never injected into agent prompts.

    Exclusions enforced by the distiller's system prompt:
      - Current portfolio percentages
      - Current FX rates
      - Specific dollar amounts at point-in-time
      - Dated tranche schedules
      - Share counts
      - Implementation roadmap "next 30/90 days" sections
    """

    plan_label: str
    distilled_at_iso: str  # ISO-8601 UTC

    goals: list[Goal] = Field(default_factory=list)
    principles: list[Principle] = Field(default_factory=list)
    risk_priorities: list[str] = Field(
        default_factory=list,
        description="Ordered list of top risks; first item dominates."
    )
    decision_rules: list[DecisionRule] = Field(default_factory=list)
    targets: list[Target] = Field(default_factory=list)
    constraints: list[Constraint] = Field(default_factory=list)
    stress_tolerance: str = ""


__all__ = [
    "Goal",
    "Principle",
    "DecisionRule",
    "Target",
    "TargetUnit",
    "Constraint",
    "PlanDistillate",
]
