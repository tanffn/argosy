"""Pydantic schema for EquityCompAnalystAgent (Phase 5 topic owner).

Owns the RSU/equity-comp projection: contractual vesting on file vs
discretionary refresh grants, per-year gross/net under three scenarios
(known-grants-only, conservative-decay, optimistic-flat), and an
FI-date sensitivity per scenario.

Pydantic v2 syntax throughout.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from argosy.agents.base import ConfidenceBand


def _coerce_to_string(v: Any) -> str:
    """Coerce dict/list output from LLM into a string.

    The Pydantic schema requires strings for narrative fields (sell policy,
    advisor questions, cited sources), but LLMs naturally tend to return
    structured objects like ``{"locator": ..., "note": ...}``. Coerce
    rather than reject: format the dict/list as JSON and let the
    downstream consumer (synthesizer) handle the rendering.
    """
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        # Common case: {"locator": ..., "note": ...} → "locator — note"
        if "locator" in v and ("note" in v or "description" in v):
            note = v.get("note") or v.get("description") or ""
            return f"{v['locator']} — {note}" if note else str(v["locator"])
        # Fallback: JSON-serialize for visibility
        import json
        return json.dumps(v, ensure_ascii=False, default=str)
    if isinstance(v, list):
        return "; ".join(_coerce_to_string(x) for x in v)
    return str(v)


class GrantRow(BaseModel):
    """A single RSU grant on the household's books.

    ``status`` separates contractual grants already issued (high
    confidence — they vest on a known schedule) from discretionary
    refresh grants the agent is modelling for future years (low
    confidence — they require employer policy verification).
    """

    award_id: str = Field(
        description=(
            "Schwab/E*TRADE award id (the canonical identifier on the "
            "user's broker statement)."
        ),
    )
    award_date: date = Field(
        description="Date the grant was issued (YYYY-MM-DD).",
    )
    quarterly_shares: float = Field(
        description=(
            "Recurring quarterly vest count in shares. For NVIDIA grants "
            "this is 1/16 of the total grant size (4-year quarterly "
            "vesting). Float so we can represent fractional modelled "
            "refresh grants."
        ),
    )
    remaining_quarters: int = Field(
        description=(
            "Number of quarterly vests still ahead on this grant "
            "(0 means fully vested)."
        ),
    )
    status: Literal["contractual", "discretionary_refresh"] = Field(
        description=(
            "'contractual' = grant already issued + on the user's "
            "schedule. 'discretionary_refresh' = modelled future grant "
            "that depends on employer refresh policy."
        ),
    )


class YearVestRow(BaseModel):
    """One year of projected RSU vesting in one scenario."""

    year: int = Field(description="Calendar year (YYYY).")
    gross_shares: float = Field(
        description=(
            "Total NVDA shares projected to vest in this year (sum of "
            "the four quarterly vests across all contractual + modelled "
            "grants in scope for this scenario)."
        ),
    )
    gross_usd: float = Field(
        description=(
            "Gross USD value = gross_shares × assumed NVDA price. "
            "Agent must state the assumed price in scenario "
            "``assumptions_md``."
        ),
    )
    gross_nis: float = Field(
        description=(
            "Gross NIS value at the assumed FX rate (from the FX "
            "analyst's output, or a baseline rate the agent declares in "
            "``assumptions_md``)."
        ),
    )
    net_nis: float = Field(
        description=(
            "Net-of-tax NIS after the marginal IL rate + surtax + "
            "Section 102 split provided by the tax analyst's report."
        ),
    )
    net_retention_pct: float = Field(
        description="net_nis / gross_nis as a percentage, 0-100.",
    )
    confidence: Literal["HIGH", "MEDIUM", "LOW"] = Field(
        description=(
            "Per-year confidence band. HIGH for years dominated by "
            "contractual vesting; degrades as the row depends more on "
            "modelled refresh grants."
        ),
    )
    source: Literal["contractual", "modeled_refresh"] = Field(
        description=(
            "'contractual' when this year's vests come exclusively from "
            "grants already on file; 'modeled_refresh' when any portion "
            "depends on assumed future refresh grants."
        ),
    )


class ScenarioProjection(BaseModel):
    """One of the three RSU-projection scenarios.

    The three scenario ``name``s are the contract:
      * ``known_grants_only``    — conservative floor; only the active
        grants on file vest. No refresh grants modelled.
      * ``conservative_decay``   — known grants + refresh grants at 55%
        of base salary (NVIDIA 2026 refresh-grant cut per Blind / weak
        evidence; flag accordingly).
      * ``optimistic_flat``      — known grants + refresh grants at 90%
        of base (historical 2024-2025 level); roughly flat ₪500k/yr
        net through 2031.
    """

    name: Literal[
        "known_grants_only", "conservative_decay", "optimistic_flat",
    ] = Field(
        description="Which of the three contract scenarios this row models.",
    )
    assumptions_md: str = Field(
        default="",
        description=(
            "Markdown bullet list of the scenario's assumptions: NVDA "
            "price path, FX rate, marginal IL tax rate + surtax + "
            "Section 102 split, and (for scenarios 2 + 3) the refresh-"
            "grant magnitude + cadence assumption. Must call out weak-"
            "evidence assumptions explicitly."
        ),
    )

    @field_validator("assumptions_md", mode="before")
    @classmethod
    def _coerce_assumptions_md(cls, v: Any) -> str:
        if v is None:
            return ""
        return _coerce_to_string(v)
    years: list[YearVestRow] = Field(
        default_factory=list,
        description=(
            "One row per calendar year in the 2026-2031 projection "
            "horizon. Must be sorted by year ascending."
        ),
    )
    five_year_avg_net_nis: float = Field(
        default=0.0,
        description=(
            "Mean of ``net_nis`` across the years[] list. The synthesizer "
            "reads this as the headline number for the scenario."
        ),
    )
    fi_date_impact_years: float = Field(
        default=0.0,
        description=(
            "Estimated shift in the household's FI / retirement date "
            "under this scenario versus the user's baseline plan. "
            "Positive = retirement pushed LATER (less RSU income); "
            "negative = pulled EARLIER. Whole or fractional years."
        ),
    )
    confidence: Literal["HIGH", "MEDIUM", "LOW"] = Field(
        description=(
            "Overall confidence band for this scenario. HIGH only when "
            "the scenario depends exclusively on contractual grants "
            "with verified vest dates."
        ),
    )


class EquityCompAnalystOutput(BaseModel):
    """Structured output of EquityCompAnalystAgent.

    The three scenarios are produced unconditionally. ``confidence``
    on the top level summarises the agent's overall belief in the
    projection — typically MEDIUM (HIGH only when the household has
    pages 2-4 of the RSU portal on file so the active-grant list is
    verified).
    """

    model_config = ConfigDict(extra="forbid", validate_default=True)

    active_grants: list[GrantRow] = Field(
        default_factory=list,
        description=(
            "All RSU grants on the household's books with status set to "
            "'contractual'. Modelled future refresh grants live INSIDE "
            "each ScenarioProjection (via the per-year ``source`` field) "
            "rather than here — this list is restricted to grants that "
            "actually exist."
        ),
    )
    scenarios: list[ScenarioProjection] = Field(
        default_factory=list,
        description=(
            "Exactly three scenarios: known_grants_only, "
            "conservative_decay, optimistic_flat. The synthesizer reads "
            "all three so it can present the range honestly to the user."
        ),
    )
    nvda_sell_on_vest_policy: str = Field(
        default="",
        description=(
            "Markdown recommendation for the NVDA-sell-on-vest policy. "
            "Default posture per binding-policy is 'defer the sell with "
            "a cap-band rebalance trigger' (don't auto-liquidate at "
            "vest, but exit if NVDA crosses its concentration cap). "
            "Agent justifies + qualifies."
        ),
    )
    advisor_intake_questions: list[str] = Field(
        default_factory=list,
        description=(
            "Questions the Advisor agent should queue for the user when "
            "the projection has gaps. Typical entries: 'upload pages 2-4 "
            "of the RSU portal so I can verify the full active-grant "
            "list' or 'confirm next-year refresh-grant magnitude with "
            "your manager'."
        ),
    )
    confidence: ConfidenceBand = Field(
        default=ConfidenceBand.MEDIUM,
        description=(
            "Top-level confidence band. HIGH only when pages 2-4 of the "
            "RSU portal are on file (verified active-grant list) AND "
            "the tax + FX inputs were available. LOW when active-grant "
            "list is incomplete or refresh-grant policy is unknown."
        ),
    )
    cited_sources: list[str] = Field(
        default_factory=list,
        description=(
            "Source locators backing the projection — typically "
            "``identity_yaml.rsu_grants``, the tax analyst's report, "
            "the FX analyst's report, and any external sources used "
            "for the refresh-grant assumptions (e.g. 'Blind 2026 "
            "NVIDIA refresh thread')."
        ),
    )

    @field_validator("nvda_sell_on_vest_policy", mode="before")
    @classmethod
    def _coerce_sell_policy(cls, v: Any) -> str:
        if v is None:
            return ""
        return _coerce_to_string(v)

    @field_validator("advisor_intake_questions", mode="before")
    @classmethod
    def _coerce_intake_questions(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, list):
            return [_coerce_to_string(item) for item in v]
        return [_coerce_to_string(v)]

    @field_validator("cited_sources", mode="before")
    @classmethod
    def _coerce_cited_sources(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, list):
            return [_coerce_to_string(item) for item in v]
        return [_coerce_to_string(v)]

    @model_validator(mode="after")
    def _exactly_three_canonical_scenarios(self) -> "EquityCompAnalystOutput":
        """Enforce: exactly the three canonical scenario names, no duplicates.

        Codex R5 MAJOR: defaulting ``scenarios=[]`` and trusting the LLM
        to fill it lets a 23KB schema-valid response ship a structurally
        incomplete projection (missing optimistic_flat, etc.).
        """
        required = {"known_grants_only", "conservative_decay", "optimistic_flat"}
        present = {s.name for s in self.scenarios}
        missing = required - present
        if missing:
            raise ValueError(
                "EquityCompAnalystOutput.scenarios is missing required "
                f"names: {sorted(missing)}. All three "
                "(known_grants_only, conservative_decay, optimistic_flat) "
                "must be produced — the synthesizer reads all three for "
                "the headline RSU range."
            )
        counts: dict[str, int] = {}
        for s in self.scenarios:
            counts[s.name] = counts.get(s.name, 0) + 1
        dupes = sorted(n for n, c in counts.items() if c > 1)
        if dupes:
            raise ValueError(
                "EquityCompAnalystOutput.scenarios has duplicate "
                f"names: {dupes}. Each scenario name must appear exactly "
                "once."
            )
        return self


__all__ = [
    "EquityCompAnalystOutput",
    "GrantRow",
    "ScenarioProjection",
    "YearVestRow",
]
