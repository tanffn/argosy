"""Types emitted by plan_synthesis_flow (Wave 2).

Mirrors spec §4.5. Each synthesized plan_versions row carries one
HorizonSection per horizon (long/medium/short) plus a synthesis_inputs
provenance payload.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class SynthTarget(BaseModel):
    """A numeric target inside a HorizonSection.

    Distinct from agents.plan_distiller_types.Target so the synthesis
    pipeline can evolve targets independently of the distillate's
    targets-as-stated.
    """

    label: str
    value: float
    unit: Literal[
        "pct_of_portfolio",
        "pct_of_net_worth",
        "pct_of_liquid",
        "usd",
        "nis",
        "shares",
        "ratio",
        "years",
    ]
    stated_at: date
    revisit_after: date
    rationale: str = ""
    source_section: str | None = None


class Theme(BaseModel):
    """Qualitative tilt for a horizon.

    Examples: "Tighter NVDA cap given DeepSeek + tariffs",
    "Currency-discipline: don't panic-convert".
    """

    label: str
    direction: Literal["lean_into", "lean_away_from", "monitor"]
    rationale: str = ""
    cited_sources: list[str] = Field(default_factory=list)


class Action(BaseModel):
    """An action item for the horizon.

    horizon_kind:
      - "directional": "continue NVDA reduction toward 15%"
      - "parameterized": "if VIX > 30 OR NVDA > $250: accelerate tranche size by 50%"
      - "dated": "harvest IBIT loss before 2026-05-15"
    """

    label: str
    horizon_kind: Literal["directional", "parameterized", "dated"]
    trigger_or_date: str | None = None
    detail: str = ""
    rationale: str = ""
    cited_sources: list[str] = Field(default_factory=list)


class SpeculativeCandidate(BaseModel):
    """Bounded-risk opportunity surfaced in the short horizon.

    risk_ceiling_check MUST be True for the candidate to be surfaced;
    the synthesizer enforces this against agent_settings.yaml::
    speculation.max_pct_of_net_worth.
    """

    ticker: str
    thesis_summary: str
    suggested_position_usd: float
    suggested_position_pct_of_net_worth: float
    risk_ceiling_check: bool
    horizon_days: int
    expected_drawdown_pct: float
    exit_trigger: str
    sourced_from: list[str] = Field(default_factory=list)


class Delta(BaseModel):
    """One change in the draft vs. prior current plan.

    item_id is a stable string within a draft (e.g. "medium.targets.nvda")
    so per-delta accept/reject in the UI keys against a stable identifier.
    """

    item_kind: Literal["target", "theme", "action", "speculative_candidate"]
    item_id: str
    horizon: Literal["long", "medium", "short"]
    change_kind: Literal["added", "removed", "modified"]
    summary: str
    prior: dict | None = None
    proposed: dict | None = None
    rationale: str = ""
    cited_sources: list[str] = Field(default_factory=list)
    accepted: bool = False
    user_edited: bool = False
    user_edit_note: str | None = None


class HorizonSection(BaseModel):
    """One of the three horizon documents emitted by synthesis."""

    horizon: Literal["long", "medium", "short"]
    freshness_expected: Literal["annual", "quarterly", "monthly"]
    status: Literal["no_change", "minor_revision", "major_revision"]
    posture: str
    targets: list[SynthTarget] = Field(default_factory=list)
    themes: list[Theme] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)
    speculative_candidates: list[SpeculativeCandidate] = Field(default_factory=list)
    deltas_from_prior: list[Delta] = Field(default_factory=list)
    rationale: str = ""
    cited_sources: list[str] = Field(default_factory=list)


class SynthesisInputs(BaseModel):
    """Provenance: what was fed into this synthesis run."""

    baseline_id: int | None = None
    prior_current_id: int | None = None
    snapshot_id: int | None = None
    fill_ids: list[int] = Field(default_factory=list)
    agent_report_ids: list[int] = Field(default_factory=list)
    debate_outcome_ids: list[int] = Field(default_factory=list)
    decision_run_id: str | None = None


class PlanSynthesisOutput(BaseModel):
    """The full output of one synthesis run, written to plan_versions
    as role='draft'.
    """

    long: HorizonSection
    medium: HorizonSection
    short: HorizonSection
    inputs: SynthesisInputs


__all__ = [
    "Action",
    "Delta",
    "HorizonSection",
    "PlanSynthesisOutput",
    "SpeculativeCandidate",
    "SynthTarget",
    "SynthesisInputs",
    "Theme",
]
