"""Plan skeleton synthesizer — stage A of sliced full synthesis (phase 3).

Design: docs/design/sliced_full_synthesis.md §2.A.

The skeleton is the SMALL call that makes every cross-cutting decision the
monolith used to make inside one 30-60k-token generation: per-horizon status
/ posture stance / the full numeric targets table, the theme + action
rosters, the speculative-candidate roster (numbers included), the delta
roster (structural fields only — the roster IS the change contract), and the
section roster with the 1-3 key facts each section must state.

Everything that must agree ACROSS slices lives here; the parallel expansion
calls (``argosy/agents/plan_slice_synthesizer.py``) receive this skeleton
verbatim as the coherence contract and only write prose/evidence around it.
Deterministic assembly byte-enforces the skeleton's locked fields, so a
slice that "improves" a skeleton decision only wastes its own output.

The derivation-ownership, ID-stability, and unit-discipline rules from the
monolith prompt (``plan_synthesizer.py``) move HERE wholesale — the skeleton
is where numbers are decided, so it is where those rules bind. Expansion
rules (evidence discipline, citation formats, prose bans) live on the slice
agent.

Output ~5-8k tokens — cheap to retry, and small enough that the
malformed-JSON / sdk-timeout hazard classes effectively vanish for this
call. Gate: ``argosy/quality/skeleton_gate.py`` runs BEFORE fan-out.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, get_args, get_origin

from pydantic import BaseModel, Field, field_validator

from argosy.agents._plan_authority import AUTHORITY_DISCLAIMER, PRIME_DIRECTIVE
from argosy.agents.base import BaseAgent
from argosy.agents.plan_synthesizer_types import (
    SpeculativeCandidate,
    SynthTarget,
)
from argosy.quality.canonical_sections import CANONICAL_SECTION_IDS


def literal_values(model: type[BaseModel], field_name: str) -> tuple[str, ...]:
    """Allowed values of a ``Literal``-typed pydantic field, read from the
    model itself — the prompt's vocabulary is SCHEMA-DERIVED, so it can
    never drift from what the validator accepts (run-155 live bug: the
    hand-written prompt lists lacked vocabulary the model needed and the
    validator rejected values the prompt never showed)."""
    ann = model.model_fields[field_name].annotation
    if get_origin(ann) is not Literal:
        raise TypeError(
            f"{model.__name__}.{field_name} is not Literal-typed "
            f"(got {ann!r}) — literal_values only renders closed vocabularies"
        )
    return tuple(str(v) for v in get_args(ann))


def _allowed(model: type[BaseModel], field_name: str) -> str:
    """Render a Literal field's allowed values for the prompt:
    ``'a' | 'b' | 'c'``."""
    return " | ".join(f"'{v}'" for v in literal_values(model, field_name))


class SkeletonTheme(BaseModel):
    """Theme roster entry — the decided tilt, minus the essay."""

    label: str = Field(..., min_length=1)
    direction: Literal["lean_into", "lean_away_from", "monitor"]


class SkeletonAction(BaseModel):
    """Action roster entry — identity + trigger; detail is expansion work."""

    label: str = Field(..., min_length=1)
    horizon_kind: Literal["directional", "parameterized", "dated"]
    trigger_or_date: str | None = None


class SkeletonDelta(BaseModel):
    """One ``Delta`` minus its prose (design §2.A) — the roster IS the
    change contract; expansion may only fill rationale/prior/proposed/
    citations."""

    item_kind: Literal["target", "theme", "action", "speculative_candidate"]
    item_id: str = Field(..., min_length=1)
    horizon: Literal["long", "medium", "short"]
    change_kind: Literal["added", "removed", "modified"]
    summary: str = Field(..., min_length=1)


class SkeletonSectionEntry(BaseModel):
    """Section roster entry: which canonical section exists, where, and the
    1-3 facts (WITH values) it must state."""

    section_id: str
    horizon: Literal["long", "medium", "short"]
    one_line_thesis: str = Field(..., min_length=1)
    key_facts: list[str] = Field(default_factory=list)

    @field_validator("section_id")
    @classmethod
    def _must_be_canonical(cls, v: str) -> str:
        if v not in CANONICAL_SECTION_IDS:
            raise ValueError(
                f"SkeletonSectionEntry.section_id {v!r} is not canonical "
                f"(valid: {sorted(CANONICAL_SECTION_IDS.keys())})"
            )
        return v


class SkeletonHorizon(BaseModel):
    """Per-horizon cross-cutting decisions. ``targets`` reuses the existing
    ``SynthTarget`` model UNCHANGED (the targets table is the cross-slice
    numbers contract); ``posture_summary`` is the stance in 2-4 sentences,
    not the essay."""

    horizon: Literal["long", "medium", "short"]
    freshness_expected: Literal["annual", "quarterly", "monthly"]
    status: Literal["no_change", "minor_revision", "major_revision"]
    posture_summary: str = Field(..., min_length=1)
    targets: list[SynthTarget] = Field(default_factory=list)
    theme_roster: list[SkeletonTheme] = Field(default_factory=list)
    action_roster: list[SkeletonAction] = Field(default_factory=list)
    # Short horizon only (gate-enforced); ALL numeric fields decided here.
    speculative_candidates: list[SpeculativeCandidate] = Field(
        default_factory=list
    )


class PlanSkeleton(BaseModel):
    """The full stage-A artifact — internal phase-3 intermediate, persisted
    only as a ``decision_phases`` sub-checkpoint, never a ``plan_versions``
    surface (design §3)."""

    long: SkeletonHorizon
    medium: SkeletonHorizon
    short: SkeletonHorizon
    delta_roster: list[SkeletonDelta] = Field(default_factory=list)
    section_roster: list[SkeletonSectionEntry] = Field(default_factory=list)


class PlanSkeletonSynthesizerAgent(BaseAgent[PlanSkeleton]):
    """Stage A of sliced phase 3 — the cross-cutting decision call."""

    agent_role = "plan_synthesizer"  # same Opus-class model resolution
    output_model = PlanSkeleton
    # The skeleton carries rosters + numbers, not evidence-bearing prose;
    # citations are the SLICE expansion's contract (same reasoning as the
    # patch agents). The assembled full artifact still passes every
    # full-artifact evidence/citation gate downstream.
    require_citations = False
    use_structured_output = True
    schema_retry_attempts = 2

    def build_prompt(
        self,
        *,
        baseline_distillate_md: str,
        analyst_reports_text: str,
        debate_outcomes_text: str,
        portfolio_snapshot_summary: str,
        recent_fills_summary: str,
        speculation_cap_pct: float | None = None,
        speculation_cap_concurrent: int | None = None,
        prior_items_index: list[dict] | None = None,
        user_directive: str = "",
        resolved_numbers_block: str = "",
        gate_violations_block: str = "",
    ) -> tuple[str, str]:
        today = date.today().isoformat()
        system = (
            "You are the plan SKELETON synthesizer on the Argosy fleet — "
            "stage A of the sliced phase-3 synthesis.\n\n"
            f"{AUTHORITY_DISCLAIMER}\n\n"
            f"{PRIME_DIRECTIVE}\n\n"
            "Your job: make EVERY cross-cutting decision for the new plan "
            "draft in ONE compact artifact — the PlanSkeleton. Parallel "
            "expansion calls will write the prose and evidence around your "
            "decisions; a deterministic assembler byte-restores your locked "
            "fields, so the skeleton is the single place where numbers, "
            "statuses, and rosters are decided. Decide; do not write essays.\n\n"
            "Per-horizon character:\n"
            "  - long (5+ years): posture-heavy, few targets, directional "
            "actions, status=no_change is the common case.\n"
            "  - medium (1-2 years): the strategic centerpiece — tactical "
            "targets, themed actions, parameterized triggers.\n"
            "  - short (~30 days): dated, concrete, replaced monthly. The "
            "ONLY horizon that may carry speculative_candidates.\n\n"
            "STATUS values:\n"
            "  - no_change: nothing material moved (that horizon's delta "
            "roster MUST then be empty — gate-enforced).\n"
            "  - minor_revision: targets nudged or actions refined.\n"
            "  - major_revision: structural target/posture change.\n\n"
            "DELTA ROSTER: every change vs. the prior current plan produces "
            "ONE SkeletonDelta with a stable item_id (e.g. "
            "'medium.targets.nvda'), item_kind, horizon, change_kind, and a "
            "one-line summary. NO rationale here — expansion fills prose. "
            "The roster IS the change contract: an item changed without a "
            "roster entry, or a roster entry without a real change, both "
            "break the draft. SECTIONS ARE NOT DELTA ITEMS: the delta "
            "roster covers targets/themes/actions/speculative_candidates "
            "ONLY — a section whose content changed is expressed through "
            "the section_roster (its one_line_thesis/key_facts), never as "
            "a delta entry.\n\n"
            "ID STABILITY (structural contract):\n"
            "  - The PRIOR ITEMS INDEX block lists item_ids from earlier "
            "plan drafts. When an item you decide matches a prior item "
            "(same horizon + same intent + same target variable), REUSE its "
            "exact item_id in the delta roster.\n"
            "  - For a genuinely new item, mint a stable kebab-case id "
            "`<horizon>.<kind>.<slug>` (kind is the plural field name: "
            "targets/themes/actions/speculative_candidates). Don't bake a "
            "transient number into the slug unless truly year-anchored.\n"
            "  - When DROPPING a prior item, emit change_kind='removed' "
            "with its ORIGINAL item_id; never silently omit it.\n\n"
            "DERIVATION OWNERSHIP (HARD RULE — gate-enforced BEFORE any "
            "expansion runs):\n"
            "  You are FORBIDDEN from inventing NVDA concentration target "
            "percentages, retirement years, FI thresholds, or asset class "
            "targets. These MUST come from analyst outputs. When a "
            "`DERIVED HEADLINE NUMBERS` block is present it is "
            "AUTHORITATIVE: use its exact values for every headline figure "
            "and never substitute a rounded or carried-forward number. A "
            "deterministic gate checks every headline target against those "
            "derived values before ANY expansion tokens are spent; a "
            "mismatch aborts your skeleton. If an analyst hasn't produced a "
            "value, state `[derivation pending]` in the relevant section "
            "key_facts rather than picking a number.\n"
            "  HARD FACTS vs SOFT REFERENCE: derive every target and rate "
            "from the HARD FACTS section (holdings, analyst outputs, "
            "DERIVED HEADLINE NUMBERS) + the goal. SOFT REFERENCE (baseline "
            "plan, prior items, past fills) is for continuity and item_id "
            "lineage ONLY — never carry a number or target forward from it; "
            "a past sale cadence is HISTORY, not a target.\n\n"
            "UNIT DISCIPLINE (SynthTarget): for a RATE (SWR, expected/real "
            "return, yield, marginal tax) use unit='pct' with the value AS "
            "the percent — a 3% SWR is {value: 3.0, unit: 'pct'}. NEVER tag "
            "a rate as 'ratio' ('ratio' is ONLY for true multiples like a "
            "2.5x coverage ratio). Allocation weights use "
            "'pct_of_portfolio'/'pct_of_net_worth'/'pct_of_liquid'. "
            f"stated_at is {today}; set an honest revisit_after per horizon.\n\n"
            "SECTION ROSTER: list every canonical section the plan will "
            "carry — (section_id, horizon, one_line_thesis, key_facts). "
            "section_id MUST be EXACTLY one of the "
            f"{len(CANONICAL_SECTION_IDS)} canonical ids (no other id "
            "exists — never invent one; e.g. governance content belongs "
            "under 'ips'):\n  "
            + ", ".join(f"'{s}'" for s in sorted(CANONICAL_SECTION_IDS))
            + "\n"
            f"Aim for >=12 of the {len(CANONICAL_SECTION_IDS)} canonical "
            "section_ids (gate-enforced "
            "floor); the same section_id may appear in multiple horizons. "
            "key_facts are the 1-3 facts (WITH values, taken from the "
            "DERIVED HEADLINE NUMBERS / analyst outputs) that section must "
            "state — the expansion call treats them as its content "
            "contract.\n\n"
            "OUTPUT SHAPE (the SDK enforces the schema strictly):\n"
            "  PlanSkeleton = {\n"
            "    long / medium / short: SkeletonHorizon,\n"
            "    delta_roster: SkeletonDelta[],\n"
            "    section_roster: SkeletonSectionEntry[]\n"
            "  }\n"
            "  SkeletonHorizon = {horizon, freshness_expected: "
            f"{_allowed(SkeletonHorizon, 'freshness_expected')}, "
            "status, posture_summary "
            "(2-4 sentences — the stance, not the essay), targets: "
            "SynthTarget[], theme_roster: {label, direction}[] where "
            "direction is EXACTLY one of "
            f"{_allowed(SkeletonTheme, 'direction')} "
            "(NEVER 'maintain'/'reduce'/'hold' — map: keep-as-is "
            "-> 'monitor', trim/exit -> 'lean_away_from', add -> "
            "'lean_into'), "
            "action_roster: {label, horizon_kind, trigger_or_date?}[] "
            "where horizon_kind is EXACTLY one of "
            f"{_allowed(SkeletonAction, 'horizon_kind')} "
            "(the KIND of action, NEVER the "
            "horizon name 'long'/'medium'/'short': no-numbers stance -> "
            "'directional', carries a number/threshold -> 'parameterized', "
            "tied to a calendar date -> 'dated'), "
            "speculative_candidates: SpeculativeCandidate[] (short only)}\n"
            "  SynthTarget = {label, value: number, unit, stated_at: "
            "'YYYY-MM-DD', revisit_after: 'YYYY-MM-DD', rationale: '' "
            "(LEAVE EMPTY — expansion writes it), source_section?, "
            "snapshot_category?}\n"
            "  SkeletonDelta = {item_kind: EXACTLY one of "
            f"{_allowed(SkeletonDelta, 'item_kind')} "
            "(NEVER 'section' — section changes go through the "
            "section_roster, not the delta roster), item_id, "
            "horizon, change_kind: EXACTLY one of "
            f"{_allowed(SkeletonDelta, 'change_kind')} "
            "(NEVER 'revised'/'updated'/'changed' — any edit "
            "to an existing item is 'modified'), summary}\n"
            "  (horizon status is EXACTLY one of "
            f"{_allowed(SkeletonHorizon, 'status')}.)\n"
            "  SkeletonSectionEntry = {section_id, horizon, "
            "one_line_thesis, key_facts: string[]}\n"
        )

        if speculation_cap_pct is not None:
            system += (
                "\n\nSPECULATION CAP (HARD CONSTRAINT):\n"
                f"  - max position size: {speculation_cap_pct:.4f} of net "
                f"worth (= {speculation_cap_pct * 100:.2f}%)\n"
                f"  - max concurrent positions: {speculation_cap_concurrent}\n"
                "Every SpeculativeCandidate must have "
                "suggested_position_pct_of_net_worth <= the cap AND "
                "risk_ceiling_check=true; candidates over the cap are "
                "dropped deterministically."
            )

        if user_directive:
            system += (
                "\n\nUSER DIRECTIVE PRESENT: a USER DIRECTIVE block appears "
                "in the user message. Treat it as authoritative human input; "
                "bake AGREED objections in, use DISAGREED counter-positions "
                "as the target, re-evaluate DEFERRED honestly. Corrective "
                "CORRECTIONS/DIRECTIVES listed there must be resolved BY "
                "YOUR SKELETON — the canonical values belong in your "
                "targets/key_facts; the wrong values must not appear "
                "anywhere in your output."
            )

        # Prior-items index — same rendering as the monolith prompt.
        if prior_items_index:
            by_horizon: dict[str, list[dict]] = {
                "long": [], "medium": [], "short": [],
            }
            for it in prior_items_index:
                h = (it.get("horizon") or "").lower()
                if h in by_horizon:
                    by_horizon[h].append(it)
            lines: list[str] = []
            for h in ("long", "medium", "short"):
                items = by_horizon[h]
                if not items:
                    continue
                lines.append(f"  [{h}]")
                for it in items:
                    src = it.get("from_plan", "")
                    suffix = f"  (from plan #{src})" if src else ""
                    lines.append(
                        f"    - {it.get('item_id', '?')}  "
                        f"({it.get('item_kind', '')})  "
                        f"label={it.get('label', '')!r}  "
                        f"value={it.get('value', '')} "
                        f"{it.get('unit', '')}{suffix}"
                    )
            prior_items_block = "\n".join(lines) if lines else "  (none)"
        else:
            prior_items_block = (
                "  (no prior items — first synthesis for this user)"
            )

        sections: list[str] = []
        if user_directive:
            sections.append(
                "=== USER DIRECTIVE (authoritative human input on this run) "
                "===\n" + user_directive
            )
        if gate_violations_block:
            sections.append(
                "=== SKELETON GATE VIOLATIONS (your previous skeleton FAILED "
                "the deterministic gate — fix EVERY item below) ===\n"
                + gate_violations_block
            )
        if resolved_numbers_block:
            sections.append(
                "=== DERIVED HEADLINE NUMBERS (AUTHORITATIVE — USE VERBATIM) "
                "===\n" + resolved_numbers_block
            )
        sections.extend([
            "================ HARD FACTS — GROUND TRUTH ================\n"
            "Derive EVERY target and rate from THESE + the goal.",
            "=== PORTFOLIO SNAPSHOT (current holdings) ===\n"
            + portfolio_snapshot_summary,
            "=== ANALYST REPORTS (Phase 1 outputs) ===\n"
            + analyst_reports_text,
            "=== DEBATE OUTCOMES (Phase 2 outputs, one per horizon) ===\n"
            + debate_outcomes_text,
            "================ SOFT REFERENCE — HISTORY (NOT a source of "
            "targets) ========\n"
            "For continuity, narrative framing, and item_id lineage ONLY. "
            "Never carry a number or target forward from this section.",
            "=== BASELINE PLAN [reference only] ===\n"
            + (baseline_distillate_md or "(no baseline)"),
            "=== PRIOR ITEMS INDEX [reference — item_id stability only] ===\n"
            + prior_items_block,
            "=== RECENT FILLS + DECISIONS — past execution, last 90 days "
            "[reference] ===\n" + recent_fills_summary,
            "Produce the PlanSkeleton JSON now. Decide everything "
            "cross-cutting; leave rationale prose EMPTY. If status="
            "no_change for a horizon, its delta roster must be empty.\n\n"
            "RESPONSE FORMAT: emit the JSON object directly — no markdown "
            "code fences, no preamble. Your response MUST start with `{` "
            "and END with `}`.",
        ])
        return system, "\n\n".join(sections)


__all__ = [
    "PlanSkeleton",
    "PlanSkeletonSynthesizerAgent",
    "SkeletonAction",
    "SkeletonDelta",
    "SkeletonHorizon",
    "SkeletonSectionEntry",
    "SkeletonTheme",
    "literal_values",
]
