"""Pydantic schema for ConcentrationAnalystAgent (NVDA cap derivation).

The concentration analyst OWNS NVDA concentration derivation. Per Codex
audit (drun 71), earlier waves let the synthesizer pick the NVDA target
percentage out of the air — a 15% target appeared in plan v20 with no
analyst report backing it. This module defines the structured output
schema the agent must emit so the synthesizer can READ a derived cap
instead of inventing one.

Pydantic v2 syntax throughout.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from argosy.agents.base import ConfidenceBand


def _coerce_to_string(v: Any) -> str:
    """Coerce dict/list LLM output back into a string for narrative fields."""
    if isinstance(v, str):
        return v
    if isinstance(v, (dict, list)):
        import json
        return json.dumps(v, ensure_ascii=False, default=str)
    if v is None:
        return ""
    return str(v)


def _coerce_confidence(v: Any) -> str:
    """Normalize free-form confidence indicators back into HIGH/MEDIUM/LOW.

    Unknown / missing / malformed → LOW (codex R5 BLOCKER: silently
    upgrading to MEDIUM hides invalid output).
    """
    if v is None:
        return "LOW"
    if isinstance(v, str):
        up = v.strip().upper()
        if up in {"HIGH", "MEDIUM", "LOW"}:
            return up
        if up in {"H"}: return "HIGH"
        if up in {"M", "MED"}: return "MEDIUM"
        if up in {"L"}: return "LOW"
    return "LOW"


# The four constraints the analyst MUST derive and MIN() over. Declared as
# a Literal so a typo on either side (agent prompt → output, or test →
# Pydantic) fails loudly rather than silently dropping a constraint.
ConstraintName = Literal[
    "sequence_cap",
    "tail_loss_cap",
    "risk_contribution_cap",
    "tax_liquidity_cap",
]


class ConstraintRow(BaseModel):
    """One of the four derivation constraints whose MIN sets the NVDA cap.

    See ``ConcentrationAnalystOutput.nvda_cap_pct`` for how the four
    rows combine. Each row carries its own derivation_md + confidence so
    the synthesizer (and a human reading the plan) can SEE why a
    constraint binds, not just what the cap number is.
    """

    name: ConstraintName = Field(
        description=(
            "Which of the four constraints this row models: "
            "sequence_cap (FI-date delay tolerance), tail_loss_cap "
            "(p5 portfolio loss vs max-drawdown tolerance), "
            "risk_contribution_cap (marginal-variance contribution), "
            "tax_liquidity_cap (per-year sale capacity given Section 102 "
            "windows + surtax cost)."
        ),
    )
    value_pct: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Cap value for THIS constraint as a fraction of the tradeable "
            "portfolio (0.0–1.0). The agent's final nvda_cap_pct is the "
            "MIN over the four constraint rows."
        ),
    )
    derivation_md: str = Field(
        default="",
        description=(
            "Markdown explanation of how this constraint was derived: "
            "inputs (σ_NVDA, correlation, tax rates, FI date, etc.), "
            "the math, and the value that fell out. Must be specific "
            "enough that a human can re-derive the row."
        ),
    )
    confidence: Literal["HIGH", "MEDIUM", "LOW"] = Field(
        default="MEDIUM",
        description=(
            "Confidence band for this constraint's inputs. LOW when a "
            "critical input (e.g. user delay tolerance) had to be "
            "assumed; HIGH when every input is sourced from a live "
            "analyst report or the portfolio snapshot."
        ),
    )

    @field_validator("derivation_md", mode="before")
    @classmethod
    def _coerce_derivation_md(cls, v: Any) -> str:
        return _coerce_to_string(v)

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence_field(cls, v: Any) -> str:
        return _coerce_confidence(v)


class DelaySensitivityRow(BaseModel):
    """NVDA cap at one user-stated FI-delay tolerance.

    Required rows: 0 / 1 / 2 years. The synthesizer uses these to show
    the user how cap MOVES with tolerance — the cap is a sensitivity,
    not a single number to be argued about.
    """

    delay_tolerance_years: float = Field(
        ge=0.0,
        description=(
            "Number of years the user is willing to push out the FI "
            "date in exchange for additional NVDA upside exposure. 0 = "
            "no tolerance, force cap to 0%. 1 / 2 = looser caps."
        ),
    )
    nvda_cap_pct: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "Derived NVDA cap (0.0–1.0) at this delay tolerance. Same "
            "MIN-over-four-constraints derivation as the headline cap; "
            "the only thing changing is the sequence_cap input."
        ),
    )
    rationale_md: str = Field(
        default="",
        description=(
            "Markdown: which constraint binds at this tolerance, and "
            "the rough math that puts the cap where it landed."
        ),
    )

    @field_validator("rationale_md", mode="before")
    @classmethod
    def _coerce_rationale_md(cls, v: Any) -> str:
        return _coerce_to_string(v)


class ConcentrationAnalystOutput(BaseModel):
    """Structured output of the NVDA cap derivation.

    Hard rule: the analyst NEVER accepts a target_pct from the
    synthesizer. The synthesizer reads ``nvda_cap_pct`` from this output
    and writes it onto its plan; if no analyst output is on file, the
    synthesizer must write '[derivation pending]' instead of guessing.
    """

    model_config = ConfigDict(extra="forbid", validate_default=True)

    current_nvda_pct: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Current NVDA share of the tradeable portfolio as a "
            "fraction (0.0–1.0). Sourced from portfolio_snapshot."
        ),
    )
    current_risk_contribution_pct: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Current NVDA marginal contribution to portfolio variance, "
            "expressed as a fraction of total portfolio variance "
            "(0.0–1.0). Formula: w_NVDA × σ_NVDA × (ρ × σ_core + "
            "w_NVDA × σ_NVDA) / σ_portfolio². Show derivation in the "
            "appropriate ConstraintRow.derivation_md."
        ),
    )
    tail_loss_p5_1y_pct: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "1-year p5 NVDA-driven loss as a fraction of TOTAL portfolio "
            "value (0.0–1.0). Uses σ from the sigma_calibrator service "
            "or 90-day historical, lognormal tail. The agent should also "
            "report p10 and p25 in sell_down_glidepath_md or "
            "delay_sensitivities for context."
        ),
    )
    constraints: list[ConstraintRow] = Field(
        default_factory=list,
        description=(
            "Exactly the four constraints: sequence_cap, tail_loss_cap, "
            "risk_contribution_cap, tax_liquidity_cap. The headline "
            "``nvda_cap_pct`` is the MIN over these four. Missing any of "
            "them is a schema-validation failure (see the validator on "
            "this field): the cap is meaningless without all four."
        ),
    )
    nvda_cap_pct: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "The derived NVDA concentration cap as a fraction (0.0–1.0). "
            "MUST equal MIN(constraints[*].value_pct). The "
            "synthesizer reads THIS field and writes it onto its medium-"
            "horizon NVDA target; the synth is FORBIDDEN from inventing "
            "its own number."
        ),
    )
    delay_sensitivities: list[DelaySensitivityRow] = Field(
        default_factory=list,
        description=(
            "Cap-at-tolerance sensitivities. Must include at minimum the "
            "0 / 1 / 2 year rows so the synthesizer (and the user) can "
            "see how the cap shifts with their stated delay tolerance."
        ),
    )
    sell_down_glidepath_md: str = Field(
        default="",
        description=(
            "Markdown: per-quarter NVDA sell sequence assuming Section "
            "102 24-month windows are checked per-lot. Show realized "
            "USD, gross NIS, net NIS after surtax-active 30% effective "
            "CGT (25% capital + 3% general + 2% capital surtax). "
            "Confirm or contradict the medium.md's 3,000/yr cadence."
        ),
    )
    advisor_intake_questions: list[str] = Field(
        default_factory=list,
        description=(
            "User-facing questions to queue when a material input is "
            "missing. Typical entries: 'what's your max FI-delay "
            "tolerance in years?' or 'what's your single-name max-"
            "drawdown tolerance as a % of portfolio?'."
        ),
    )
    confidence: ConfidenceBand = Field(
        default=ConfidenceBand.MEDIUM,
        description=(
            "Top-level confidence band. HIGH only when σ + correlation + "
            "user delay tolerance + max-drawdown tolerance are all on "
            "file. LOW when delay/drawdown tolerances were assumed."
        ),
    )
    cited_sources: list[str] = Field(
        default_factory=list,
        description=(
            "Source locators backing the derivation: "
            "``portfolio_snapshot:NVDA``, ``sigma_calibrator.NVDA``, "
            "``tax_analyst.effective_cgt``, "
            "``withdrawal_sequencer.fi_year``, "
            "``equity_comp_analyst.scenarios``, etc."
        ),
    )

    @field_validator("constraints")
    @classmethod
    def _all_four_constraints_required(
        cls, value: list[ConstraintRow]
    ) -> list[ConstraintRow]:
        """All four named constraints must appear exactly once.

        The synthesizer reads ``nvda_cap_pct`` blindly as the binding
        cap. If a constraint row is missing, the MIN() collapses to
        whatever rows are present, silently relaxing the cap. Fail loud
        instead. With ``validate_default=True`` on the model config,
        this validator fires even when ``constraints`` is omitted (and
        the empty default would have sneaked past in Pydantic v2 with
        the default config) — codex R5 BLOCKER.
        """
        required = {
            "sequence_cap",
            "tail_loss_cap",
            "risk_contribution_cap",
            "tax_liquidity_cap",
        }
        present = {row.name for row in value}
        missing = required - present
        if missing:
            raise ValueError(
                "ConcentrationAnalystOutput.constraints is missing "
                f"required rows: {sorted(missing)}. All four constraints "
                "(sequence_cap, tail_loss_cap, risk_contribution_cap, "
                "tax_liquidity_cap) must be derived — MIN() over a "
                "partial set silently relaxes the cap."
            )
        # Each name exactly once.
        counts: dict[str, int] = {}
        for row in value:
            counts[row.name] = counts.get(row.name, 0) + 1
        dupes = sorted(n for n, c in counts.items() if c > 1)
        if dupes:
            raise ValueError(
                "ConcentrationAnalystOutput.constraints has duplicate "
                f"rows for: {dupes}. Each constraint name must appear "
                "exactly once."
            )
        return value

    @model_validator(mode="after")
    def _nvda_cap_equals_min_constraints(self) -> "ConcentrationAnalystOutput":
        """``nvda_cap_pct`` MUST equal MIN(constraints[*].value_pct).

        Codex R5 BLOCKER: the schema description said this but the code
        didn't enforce it, letting the agent emit a relaxed headline cap
        higher than the binding constraint.
        """
        if not self.constraints:
            return self
        min_cap = min(c.value_pct for c in self.constraints)
        if abs(self.nvda_cap_pct - min_cap) > 1e-6:
            raise ValueError(
                f"nvda_cap_pct={self.nvda_cap_pct:.4f} must equal "
                f"MIN(constraints[*].value_pct)={min_cap:.4f}. The "
                "synthesizer reads nvda_cap_pct as the binding cap; a "
                "relaxed headline that drifts above any constraint "
                "would silently let through over-cap NVDA exposure."
            )
        return self


__all__ = [
    "ConstraintName",
    "ConstraintRow",
    "DelaySensitivityRow",
    "ConcentrationAnalystOutput",
]
