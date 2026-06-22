"""Types emitted by plan_synthesis_flow (Wave 2).

Mirrors spec §4.5. Each synthesized plan_versions row carries one
HorizonSection per horizon (long/medium/short) plus a synthesis_inputs
provenance payload.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from argosy.quality.canonical_sections import CANONICAL_SECTION_IDS


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
        # Generic percentage for RATES (SWR, expected return, yield, marginal
        # tax) where the value IS the percent — e.g. a 3% SWR is value=3.0
        # unit='pct'. Without this, rates were mis-tagged unit='ratio' value=3.0
        # and rendered as the nonsensical "3.0 ratio" (codex residual). 'ratio'
        # is reserved for true multiples (coverage 2.5×), NOT for percentages.
        "pct",
        "usd",
        "nis",
        "shares",
        "ratio",
        "years",
        # Time-window units the synthesizer reaches for in practice. Run
        # #22 was killed by long.targets[1].unit='months' (a "revisit in
        # 12 months" target); 'days' rounds out the short-horizon needs
        # so a "review in 30 days" target validates instead of crashing.
        "months",
        "days",
    ]
    stated_at: date
    revisit_after: date
    rationale: str = ""
    source_section: str | None = None
    # Explicit portfolio-snapshot category this target anchors to (B1/H5). When
    # present the allocation glidepath uses it for an EXACT today-value match
    # instead of fragile label-substring alias routing (which mis-routed e.g.
    # "US growth tilt (ex-NVDA)" onto the "nvda" keyword). Sourced from
    # AllocationClass.snapshot_category via allocation_plan.to_synth_targets.
    snapshot_category: str | None = None


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
    # Optional execution guidance surfaced on the /proposals "What's on you to
    # do" checklist. ``how_to`` = concrete steps (pointing at the right Argosy
    # surface where relevant); ``done_when`` = a crisp, checkable completion
    # criterion ("definition of done"). Both default to "" for backward
    # compatibility — older plans without them fall back to the deterministic
    # mapper in argosy.services.action_item_guidance.
    how_to: str = ""
    done_when: str = ""


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


# ---------------------------------------------------------------------------
# Phase 3 — SectionEvidence contract (v3.1 §3.2 Check 4).
#
# Pydantic v2 models. The 5 model_validators on SectionEvidence run at
# instantiation (fail-fast shape gate). The content-side semantic
# checks (numeric substring + categorical token-overlap) live in
# argosy/quality/plan_output_gate.py::_validate_section_evidence and
# run at gate-check time. Defense-in-depth on purpose — Pydantic
# catches synth-prompt drift early; the gate catches the synth model
# lying fluently.
# ---------------------------------------------------------------------------


class FactClaim(BaseModel):
    """One atomic claim a Section asserts about the household's plan.

    ``text`` is the natural-language statement (≥12 chars, no
    single-token "facts"). ``kind`` discriminates how downstream
    validation interprets the claim:

      - ``numeric``     — ``value`` must appear as substring in the
                          supporting citation extract (gate-side check).
      - ``categorical`` / ``policy`` / ``qualitative`` — extract must
                          share ≥3 content tokens with ``text``.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=12, max_length=300)
    kind: Literal["numeric", "categorical", "policy", "qualitative"]
    value: Decimal | str | None = None
    unit: str | None = None
    horizon: Literal["short", "medium", "long"] | None = None

    @field_validator("text")
    @classmethod
    def _strip_min_length(cls, v: str) -> str:
        """Reject single-token claims even when ``min_length=12`` allows
        them through padding (e.g. ``"   NVDA   "``). The strip() check
        is what v3.1 §3.2 Check 4 rule 5 demands."""
        if len(v.strip()) < 12:
            raise ValueError(
                "FactClaim.text must be >=12 chars after strip "
                "(no single-token facts)"
            )
        return v


class Citation(BaseModel):
    """One source pointer that supports a specific FactClaim.

    ``supports_fact_index`` indexes into the parent
    ``SectionEvidence.facts`` list. ``extract`` is required (≥8 chars)
    for concrete kinds (plan_doc / portfolio_snapshot / analyst_report);
    optional for soft kinds (inference / agent_baseline /
    assumption_register), which require a matching Assumption instead.

    ``source_locator`` is free-form; for distillate-derived facts the
    synth prompt requires ``distillate.<field_name>[<index>]`` so the
    Phase 4 binding gate can verify USE.
    """

    model_config = ConfigDict(extra="forbid")

    source_kind: Literal[
        "plan_doc",
        "portfolio_snapshot",
        "analyst_report",
        "assumption_register",
        "inference",
        "agent_baseline",
    ]
    source_locator: str = Field(..., min_length=1)
    extract: str | None = None
    supports_fact_index: int = Field(..., ge=0)


class Assumption(BaseModel):
    """One declared default that an inference or agent_baseline
    citation rests on. ``SectionEvidence`` requires ≥1 Assumption
    whenever any citation cites a soft source kind."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1)
    default_value: Decimal | str
    rationale: str = Field(..., min_length=1)
    can_be_overridden: bool = True


class SectionEvidence(BaseModel):
    """Structured evidence supporting a Section's body_md.

    The 5 model validators below implement the v3.1 SHAPE contract
    (plan §3.2 Check 4). ``Section`` and ``SectionEvidence`` keep
    ``extra="ignore"`` (default) so additive Phase 4 fields don't
    break in-flight Phase 3 deploys; the inner contract types
    (FactClaim, Citation, Assumption) use ``extra="forbid"`` to
    catch synth-prompt drift early.
    """

    facts: list[FactClaim] = Field(default_factory=list)
    source_span: list[Citation] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)

    # Validator declaration order matters in Pydantic v2 — the first
    # to raise short-circuits the rest. We run the structural-integrity
    # checks (range/uniqueness on citation indices) BEFORE the
    # coverage check, because an out-of-range citation is malformed
    # data; reporting "fact 0 has no citation" first would be
    # misleading when the synth's actual error was pointing at a
    # nonexistent fact slot.

    @model_validator(mode="after")
    def _facts_or_missing(self) -> SectionEvidence:
        """Rule 1 — no silent empty sections."""
        if not self.facts and not self.missing_data:
            raise ValueError(
                "SectionEvidence must have either facts or missing_data "
                "non-empty -- silent empty is forbidden (v3.1 rule 1)"
            )
        return self

    @model_validator(mode="after")
    def _unique_cite_indices_per_fact(self) -> SectionEvidence:
        """Rule 5 — supports_fact_index in-range + no duplicate
        (source_locator, fact_index) pairs.

        Runs BEFORE _every_fact_cited so malformed indices surface as
        "out of range" instead of being miscategorized as "fact has
        no citation".
        """
        for ci, c in enumerate(self.source_span):
            if c.supports_fact_index >= len(self.facts):
                raise ValueError(
                    f"Citation[{ci}].supports_fact_index="
                    f"{c.supports_fact_index} out of range "
                    f"(facts has {len(self.facts)} entries) -- "
                    "every citation must point at a real fact"
                )
        seen: set[tuple[str, int]] = set()
        for ci, c in enumerate(self.source_span):
            key = (c.source_locator, c.supports_fact_index)
            if key in seen:
                raise ValueError(
                    f"Citation[{ci}] duplicates source_locator="
                    f"{c.source_locator!r} for fact_index="
                    f"{c.supports_fact_index} -- "
                    "drop duplicate citations"
                )
            seen.add(key)
        return self

    @model_validator(mode="after")
    def _every_fact_cited(self) -> SectionEvidence:
        """Rule 2 — every FactClaim is covered by ≥1 Citation."""
        cited = {c.supports_fact_index for c in self.source_span}
        for i in range(len(self.facts)):
            if i not in cited:
                raise ValueError(
                    f"FactClaim[{i}] has no Citation in source_span "
                    f"(facts={len(self.facts)}, "
                    f"citations={len(self.source_span)}) "
                    "-- every fact must be cited (v3.1 rule 2)"
                )
        return self

    @model_validator(mode="after")
    def _inference_requires_assumption(self) -> SectionEvidence:
        """Rule 3 — soft citations require a bound Assumption.

        ``assumption_register`` is classified soft alongside
        ``inference`` and ``agent_baseline``: it points at a declared
        default rather than a concrete extract, so an Assumption
        documenting that default is required. Without this
        classification, ``source_kind=assumption_register`` would be a
        loophole — neither requiring an ``extract`` (concrete rule) nor
        an ``Assumption`` (soft rule).
        """
        soft_kinds = {"inference", "agent_baseline", "assumption_register"}
        if any(c.source_kind in soft_kinds for c in self.source_span):
            if not self.assumptions:
                raise ValueError(
                    "source_span contains inference, agent_baseline, or "
                    "assumption_register citations but assumptions is "
                    "empty -- soft sources must declare their bound "
                    "assumption (v3.1 rule 3)"
                )
        return self

    @model_validator(mode="after")
    def _concrete_source_extract(self) -> SectionEvidence:
        """Rule 4 — concrete-source Citation has extract ≥8 chars."""
        concrete = {"plan_doc", "portfolio_snapshot", "analyst_report"}
        for ci, c in enumerate(self.source_span):
            if c.source_kind in concrete:
                if not c.extract or len(c.extract) < 8:
                    raise ValueError(
                        f"Citation[{ci}] to {c.source_kind} must include "
                        f"verbatim extract >=8 chars "
                        f"(got {len(c.extract or '')}) -- "
                        "concrete sources need verifiable extracts "
                        "(v3.1 rule 4)"
                    )
        return self


class Section(BaseModel):
    """One canonical retirement-plan section bound to a horizon.

    The same ``section_id`` may appear in multiple horizons
    (e.g. ``"concentration"`` shows up in short/medium/long with
    distinct content). Coverage is counted as the set of distinct
    ``section_id`` values present anywhere across all sections.
    """

    section_id: str
    horizon: Literal["short", "medium", "long"]
    title: str = Field(..., min_length=1)
    body_md: str = Field(..., min_length=1)
    evidence: SectionEvidence

    @field_validator("section_id")
    @classmethod
    def _must_be_canonical(cls, v: str) -> str:
        """Reject non-canonical section_ids at construction time.

        Phase 0's ``check_section_coverage`` already flags unknown ids
        at gate-check time, but failing here surfaces typos to the
        synth model with a clear error message it can learn from.
        """
        if v not in CANONICAL_SECTION_IDS:
            raise ValueError(
                f"Section.section_id {v!r} is not in CANONICAL_SECTION_IDS "
                f"({len(CANONICAL_SECTION_IDS)} valid keys; see "
                "argosy/quality/canonical_sections.py). "
                f"Valid: {sorted(CANONICAL_SECTION_IDS.keys())}"
            )
        return v


# ---------------------------------------------------------------------------


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
    decision_run_id: int | None = None


class PlanSynthesisOutput(BaseModel):
    """The full output of one synthesis run, written to plan_versions
    as role='draft'.
    """

    long: HorizonSection
    medium: HorizonSection
    short: HorizonSection
    inputs: SynthesisInputs
    # Phase 3 — flat list across all three horizons; each Section
    # carries its own ``horizon`` discriminator. ``default_factory=list``
    # keeps legacy plan_versions JSON rows (no ``sections`` key)
    # round-tripping unchanged. Phase 0's check_section_coverage
    # already tolerates empty lists.
    sections: list[Section] = Field(default_factory=list)


__all__ = [
    "Action",
    "Assumption",
    "Citation",
    "Delta",
    "FactClaim",
    "HorizonSection",
    "PlanSynthesisOutput",
    "Section",
    "SectionEvidence",
    "SpeculativeCandidate",
    "SynthTarget",
    "SynthesisInputs",
    "Theme",
]
