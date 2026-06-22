"""Plan synthesizer — Phase 3 of plan_synthesis_flow.

Inputs (assembled by the orchestrator):
  - baseline distillate (markdown)
  - prior current plan (markdown — or empty on first synthesis)
  - 9 analyst reports concatenated (text)
  - 3 debate outcomes (one per horizon)
  - portfolio snapshot summary
  - recent fills + decisions summary

Output: PlanSynthesisOutput (long, medium, short HorizonSections + inputs
provenance).

Default model: Opus. Per user preference (accuracy over cost), the
synthesizer is given the most capable model in the fleet.
"""

from __future__ import annotations

from argosy.agents._plan_authority import AUTHORITY_DISCLAIMER, PRIME_DIRECTIVE
from argosy.agents.base import BaseAgent
from argosy.agents.plan_synthesizer_types import PlanSynthesisOutput


class PlanSynthesizerAgent(BaseAgent[PlanSynthesisOutput]):
    """Phase 3 of plan_synthesis_flow."""

    agent_role = "plan_synthesizer"
    output_model = PlanSynthesisOutput
    require_citations = True
    # The synthesizer's PlanSynthesisOutput is large + carries the v3.1
    # evidence-discipline rules (soft-source citations must bind an
    # assumption); the model intermittently violates them. Feed the
    # validation error back and let it self-correct rather than failing the
    # whole synthesis (BaseAgent.schema_retry_attempts).
    schema_retry_attempts = 2
    # max_tokens driven by DEFAULT_MAX_TOKENS_BY_ROLE (32000).
    # Opt into schema-constrained JSON output. Codex tandem found
    # synth #58 truncating at the markdown-fence opener with
    # effort=max; the bundled claude.exe's --json-schema path makes
    # the model emit JSON directly (no fence, no preamble), removing
    # the truncation surface AND letting us trim the system prompt's
    # raw schema dump (~5K tokens reclaimed for thinking + output).
    use_structured_output = True

    def build_prompt(
        self,
        *,
        baseline_distillate_md: str,
        prior_current_md: str,
        analyst_reports_text: str,
        debate_outcomes_text: str,
        portfolio_snapshot_summary: str,
        recent_fills_summary: str,
        speculation_cap_pct: float | None = None,
        speculation_cap_concurrent: int | None = None,
        prior_items_index: list[dict] | None = None,
        user_directive: str = "",
        resolved_numbers_block: str = "",
    ) -> tuple[str, str]:
        system = (
            "You are the plan synthesizer on the Argosy fleet — Phase 3 of the "
            "monthly synthesis flow.\n\n"
            f"{AUTHORITY_DISCLAIMER}\n\n"
            f"{PRIME_DIRECTIVE}\n\n"
            "Your job: produce three HorizonSection documents (long, medium, "
            "short) from the inputs below. The medium horizon is the strategic "
            "centerpiece — that is where the firm earns its fee. Long is mostly "
            "stable; short is mostly mechanical.\n\n"
            "Per-horizon character:\n"
            "  - long (5+ years): posture-heavy, few targets, directional "
            "    actions, status=no_change is the common case.\n"
            "  - medium (1-2 years): tactical targets, themed actions, "
            "    parameterized triggers (\"if VIX > 30: accelerate\").\n"
            "  - short (~30 days): dated, concrete, replaced every monthly "
            "    cycle. Includes speculative_candidates.\n\n"
            "STATUS values:\n"
            "  - no_change: nothing material moved; honest, evidence-backed.\n"
            "  - minor_revision: targets nudged or actions refined.\n"
            "  - major_revision: structural target/posture change.\n\n"
            "DELTAS: every change vs. the prior current plan must produce a "
            "Delta entry with a stable item_id (e.g. 'medium.targets.nvda'), "
            "rationale, and citations. Per-delta accept/reject relies on these.\n\n"
            "ID STABILITY (structural contract — not a narration cue):\n"
            "  - The PRIOR ITEMS INDEX block in the user message lists "
            "    item_ids from earlier plan drafts. When you emit a "
            "    Target / Theme / Action whose intent matches a prior "
            "    item (same horizon + same intent + same target "
            "    variable), REUSE its exact item_id.\n"
            "  - For a genuinely new item, mint a stable kebab-case id "
            "    `<horizon>.<kind>.<slug>` (e.g. "
            "    `medium.targets.nvda_share_of_portfolio_12mo`). Don't "
            "    bake a transient number into the slug unless it is "
            "    truly anchored to a year.\n"
            "  - When DROPPING a prior item, emit a Delta with "
            "    change_kind='removed' using its original item_id; do "
            "    NOT silently omit it.\n"
            "  - The item_id is structural plumbing. The prose fields "
            "    (posture, rationale, theme.rationale, action.rationale) "
            "    are forward-looking only. Do NOT narrate revisions in "
            "    prose — no `prior`, `previous`, `earlier`, `revised "
            "    from`, `preserved from`, `lineage to`, `draft #N`, "
            "    `synth #N`, `wave N`, `v2.X`, `retracted`, `superseded` "
            "    — those words are gate-banned and will block "
            "    publication.\n\n"
            "CITATIONS REQUIRED for every numeric or directional claim. Use "
            "the format `agent_report:<id>` for analyst evidence, "
            "`decision_run:<id>` for prior synthesis lineage, "
            "`domain_kb:<path>` for jurisdiction rules, "
            "`plan_section:<heading>` for baseline references, "
            "`prior_current:<id>` for diff context.\n\n"
            # Codex audit drun 71: synth invented the 15% NVDA target on
            # medium horizon (no analyst report backed the number).
            # The synthesizer integrates analyst-derived numbers; it does
            # NOT derive them itself. Hard rule below names every
            # number-class the synth has been caught inventing.
            "DERIVATION OWNERSHIP (HARD RULE — gate-enforced):\n"
            "  You are FORBIDDEN from inventing NVDA concentration target "
            "percentages, retirement years, FI thresholds, or asset class "
            "targets. These MUST come from analyst outputs:\n"
            "    - NVDA cap            ← concentration_analyst.nvda_cap_pct\n"
            "    - Retirement year     ← WithdrawalSequencerAgent\n"
            "    - FI threshold        ← WithdrawalSequencerAgent\n"
            "    - Asset class targets ← the relevant topic-owner agent\n"
            "  If an analyst hasn't produced the value, write "
            "`[derivation pending]` rather than picking a number. The "
            "synthesizer integrates; it does not derive.\n"
            "  When a `DERIVED HEADLINE NUMBERS` block is present in the user "
            "message, it is AUTHORITATIVE: use its exact values for every "
            "headline figure and never substitute a rounded or carried-forward "
            "number. A post-synthesis gate replaces any headline number that "
            "does not match these derived values with `[derivation pending]`, "
            "so inventing one only deletes your own figure.\n"
            "  HARD FACTS vs SOFT REFERENCE (HARD RULE): the user message is split "
            "into a HARD FACTS section (current holdings, analyst outputs, and the "
            "DERIVED HEADLINE NUMBERS — derive every target/rate from THESE + the "
            "goal) and a SOFT REFERENCE section (the baseline plan, prior items, and "
            "past execution/fills). SOFT REFERENCE is for continuity, narrative, and "
            "item_id lineage ONLY. NEVER carry a number or target forward from it: a "
            "target is DERIVED from HARD FACTS, never inherited from the past plan or "
            "what was previously sold (e.g. a past sale cadence like 3,000 sh/yr is "
            "HISTORY, not a target — use the DERIVED NVDA sell/target shares instead). "
            "When you cite SOFT REFERENCE, frame it as 'previously'/'what was done', "
            "never as 'the plan'.\n\n"
            "TECHNICAL-READING DISCIPLINE (gate-checked):\n"
            "  - Any symbol-level technical reading you state (RSI, MACD, "
            "moving average, price) MUST come from the CURRENT technical "
            "payload in this run's analyst outputs — never copy a technical "
            "number from the prior current plan's prose. These readings drift "
            "daily; a carried-forward value is stale and false.\n"
            "  - If the current payload does not contain the reading, do not "
            "state a number — describe the signal qualitatively or omit it.\n"
            "  - A post-synthesis gate blocks promotion when a stated reading "
            "contradicts the current payload, so a carried-forward figure only "
            "fails your own plan. NEVER justify an action (e.g. a trim/exit) "
            "with a technical reading that the current payload does not "
            "support; a hard user constraint (e.g. the UCITS-only-non-NVDA "
            "domicile mandate) is NEVER overridden or deferred by a tactical "
            "technical signal.\n\n"
            "FI FRAMING DISCIPLINE (gate-checked):\n"
            "  - NEVER restate a retired/superseded numeric value, even to say "
            "it is being dropped (no \"down from ₪21M\", \"was ₪22M\", "
            "\"replaces the ₪11.54M base\"). Reference a dropped item by its "
            "item_id/label only — the old number must not appear in prose.\n"
            "  - The FI capital target has TWO levels; keep them distinct and "
            "never conflate: the PERPETUITY BASE (funds permanent spend at the "
            "SWR) and the TOTAL CAPITAL TARGET (= perpetuity base + the "
            "separate finite-liability reserve). Do NOT call the perpetuity "
            "base 'the FI target crossed'. State honestly which level current "
            "net worth covers: if net worth exceeds the perpetuity base but is "
            "below the total target, say 'perpetuity base funded; total capital "
            "target not yet reached (short by ₪X)', never 'past FI'.\n\n"
            # Schema enforcement: the SDK is called with --json-schema
            # (per use_structured_output=True above) so the model emits
            # schema-validated JSON directly. Concise field summary
            # replaces the ~5K-token raw pydantic schema dump that
            # used to live here. Reclaimed budget went into output.
            # Field shapes verified against
            # argosy/agents/plan_synthesizer_types.py.
            "OUTPUT SHAPE (the SDK enforces the schema strictly; this "
            "summary just tells you what each field carries):\n"
            "  {\n"
            "    long: HorizonSection,\n"
            "    medium: HorizonSection,\n"
            "    short: HorizonSection,\n"
            "    inputs: SynthesisInputs  // ALWAYS emit { baseline_id: null, prior_current_id: null, snapshot_id: null, fill_ids: [], agent_report_ids: [], debate_outcome_ids: [], decision_run_id: null }. The orchestrator overwrites these fields with the real numeric IDs post-hoc. DO NOT emit audit tokens, plan labels, or any strings here — these fields are typed int|None and any string value will fail pydantic validation and kill the whole synthesis run.\n"
            "    sections: Section[]  // Phase 3 — evidence-required canonical sections; aim for >=12 of 18 (MVP), >=18 (full ship)\n"
            "  }\n"
            "HorizonSection = {\n"
            "  horizon: 'long' | 'medium' | 'short',\n"
            "  freshness_expected: 'annual' | 'quarterly' | 'monthly',\n"
            "  status: 'no_change' | 'minor_revision' | 'major_revision',\n"
            "  posture: <prose, the horizon's strategic stance>,\n"
            "  targets: SynthTarget[],\n"
            "  themes: Theme[],\n"
            "  actions: Action[],\n"
            "  speculative_candidates: SpeculativeCandidate[]  // short horizon only; obeys SPECULATION CAP\n"
            "  deltas_from_prior: Delta[],\n"
            "  rationale: <prose, why this horizon ended up where it did>,\n"
            "  cited_sources: string[]\n"
            "}\n"
            "SynthTarget = {label, value: number, unit: 'pct'|'pct_of_portfolio'|'pct_of_net_worth'|'pct_of_liquid'|'usd'|'nis'|'shares'|'ratio'|'years'|'months'|'days', stated_at: 'YYYY-MM-DD', revisit_after: 'YYYY-MM-DD', rationale: string, source_section?: string}\n"
            "  UNIT DISCIPLINE: for a RATE (SWR, expected/real return, yield, marginal tax) use unit='pct' with the value AS the percent — a 3% SWR is {value: 3.0, unit: 'pct'}, a 5% return is {value: 5.0, unit: 'pct'}. NEVER tag a rate as 'ratio' (that produced the nonsensical '3.0 ratio'). 'ratio' is ONLY for true multiples like a 2.5× coverage ratio. Allocation weights use 'pct_of_portfolio'/'pct_of_net_worth'/'pct_of_liquid'.\n"
            "Theme = {label, direction: 'lean_into'|'lean_away_from'|'monitor', rationale: string, cited_sources: string[]}\n"
            "Action = {label, horizon_kind: 'directional'|'parameterized'|'dated', trigger_or_date?: string, detail: string, rationale: string, cited_sources: string[], how_to?: string, done_when?: string}\n"
            "  ACTION GUIDANCE: for EVERY action, emit a concrete `how_to` (the precise steps the user takes, pointing at the right Argosy surface where one exists — e.g. 'Open /proposals -> Deploy your cash and accept the buy lines', 'Compare the latest payslip withholding to the §102 estimate on /retirement') and a `done_when` (a crisp, checkable completion criterion — the definition of done). Do NOT invent specific numbers in how_to/done_when; give actionable steps + a clear bar. These render directly on the user's to-do checklist.\n"
            "SpeculativeCandidate = {ticker, thesis_summary, suggested_position_usd: number, suggested_position_pct_of_net_worth: number, risk_ceiling_check: bool, horizon_days: int, expected_drawdown_pct: number, exit_trigger: string, sourced_from: string[]}\n"
            "Delta = {item_kind: 'target'|'theme'|'action'|'speculative_candidate', item_id, horizon: 'long'|'medium'|'short', change_kind: 'added'|'modified'|'removed', summary, prior?: object, proposed?: object, rationale, cited_sources: string[], accepted: bool=false, user_edited: bool=false, user_edit_note?: string}\n\n"
            # Phase 3 — Section / SectionEvidence / FactClaim / Citation
            # / Assumption + the EVIDENCE DISCIPLINE rubric. The
            # canonical 18 section_ids are enforced by Pydantic at
            # construction (see argosy/quality/canonical_sections.py).
            "Section = {\n"
            "  section_id: 'cover_assumptions' | 'client_goals' | 'net_worth' | "
            "'cashflow' | 'capital_sufficiency' | 'ips' | 'concentration' | "
            "'withdrawal' | 'monte_carlo' | 'tax_plan' | 'insurance' | "
            "'healthcare' | 'estate' | 'cross_border' | 'equity_comp' | "
            "'fi_bridge' | 'life_events' | 'action_items',\n"
            "  horizon: 'long' | 'medium' | 'short',\n"
            "  title: <human-readable section title>,\n"
            "  body_md: <forward-looking prose; no agent names; no revision history>,\n"
            "  evidence: SectionEvidence\n"
            "}\n"
            "  -- The same section_id may appear in multiple horizons "
            "(e.g. 'concentration' in short+medium+long).\n\n"
            "SectionEvidence = {\n"
            "  facts: FactClaim[],\n"
            "  source_span: Citation[],\n"
            "  assumptions: Assumption[],\n"
            "  missing_data: string[]\n"
            "}\n"
            "  -- EVERY section MUST carry evidence. If you do not have "
            "facts for the section, populate missing_data with the "
            "SPECIFIC items you could not source. A section with both "
            "facts=[] AND missing_data=[] is rejected by the schema.\n\n"
            "FactClaim = {\n"
            "  text: string (>=12 chars after strip; a COMPLETE claim, "
            "never a single token),\n"
            "  kind: 'numeric' | 'categorical' | 'policy' | 'qualitative',\n"
            "  value: Decimal | string | null  (null OK for qualitative),\n"
            "  unit?: 'NIS' | 'USD' | '%' | 'shares' | ...,\n"
            "  horizon?: 'short' | 'medium' | 'long'\n"
            "}\n\n"
            "Citation = {\n"
            "  source_kind: 'plan_doc' | 'portfolio_snapshot' | "
            "'analyst_report' | 'assumption_register' | 'inference' | "
            "'agent_baseline',\n"
            "  source_locator: string,\n"
            "  // examples: 'distillate.cashflow_phases[2]' "
            "(REQUIRED format for distillate-derived facts so the binding "
            "gate verifies USE), 'portfolio_snapshot:NVDA', "
            "'analyst_report:42:L18', 'plan_doc:H2:Tax Optimization:L405'\n"
            "  extract: string | null,\n"
            "  // REQUIRED (>=8 chars) for plan_doc / portfolio_snapshot / "
            "analyst_report. Optional for inference / agent_baseline (which "
            "require a matching Assumption instead).\n"
            "  supports_fact_index: int  // index into SectionEvidence.facts\n"
            "}\n\n"
            "Assumption = {\n"
            "  text: string,\n"
            "  default_value: Decimal | string,\n"
            "  rationale: string,\n"
            "  can_be_overridden: bool = true\n"
            "}\n\n"
            "EVIDENCE DISCIPLINE (the EvidencePerSection contract — gate enforces):\n"
            "  1. Every section emits SectionEvidence. facts AND "
            "missing_data cannot BOTH be empty — silent-empty sections "
            "are rejected.\n"
            "  2. Every FactClaim in `facts` must be covered by at least "
            "one Citation in `source_span` whose `supports_fact_index` "
            "points at that fact's slot.\n"
            "  3. Every numeric FactClaim's supporting Citation.extract "
            "MUST contain the value as a LITERAL substring. The single "
            "most common failure mode here is mismatched numeric "
            "formatting (raw int in `value`, abbreviated string in "
            "`extract`). Examples that PASS:\n"
            "    - value=277000  + extract contains '277000' or '277,000'\n"
            "    - value=3540000 + extract contains '3540000' or '3,540,000'\n"
            "    - value=0.6486  + extract contains '0.6486' or '64.86%' "
            "(if percent — pick a value+extract pair where the value "
            "string appears in the extract)\n"
            "  Examples that FAIL (the gate rejects):\n"
            "    - value=3540000 + extract='$3.54M of total liquid'  "
            "(value '3540000' is NOT in '$3.54M')\n"
            "    - value=277000  + extract='target ₪277K per year'  "
            "(value '277000' is NOT in '₪277K')\n"
            "  Fix EITHER side to match: either write value=3.54 with "
            "unit='M_USD' so the extract '$3.54M' contains '3.54', OR "
            "write extract as 'total liquid ≈ 3,540,000 NIS (≈$3.54M)' "
            "so the comma-formatted '3,540,000' contains '3540000' (the "
            "gate normalizes commas + spaces before the substring check). "
            "The gate is locale-tolerant for thousands separators "
            "(comma + non-breaking space) but NOT for unit abbreviation "
            "(M / K / B / bn). Mirror the value format in the extract.\n"
            "  4. Every categorical / policy / qualitative FactClaim's "
            "supporting Citation.extract MUST share >=3 content tokens "
            "with FactClaim.text (stopwords like the/a/is/are removed). "
            "A vaguely related extract does NOT support a confabulated "
            "claim.\n"
            "  5. If a fact derives from a distillate field, FORMAT the "
            "source_locator as `distillate.<field_name>[<index>]` so the "
            "binding gate can verify USE (not just structural presence).\n"
            "  6. If ANY Citation has source_kind in "
            "{'inference', 'agent_baseline'}, the SectionEvidence MUST "
            "carry >=1 Assumption that documents the bound default value "
            "and its rationale.\n"
            "  7. If a section's data is unavailable, populate "
            "`missing_data` with SPECIFIC items (e.g. 'no Schwab tax-lot "
            "CSV for 2025-09 grants') — never a generic apology.\n"
            "  8. FactClaim.text must be >=12 chars after strip. 'NVDA' "
            "alone is not a fact; 'NVDA position is 22.3% of liquid net "
            "worth' is.\n\n"
        )

        if speculation_cap_pct is not None:
            cap_block = (
                "\n\nSPECULATION CAP (HARD CONSTRAINT):\n"
                f"  - max position size: {speculation_cap_pct:.4f} of net worth "
                f"(= {speculation_cap_pct*100:.2f}%)\n"
                f"  - max concurrent positions: {speculation_cap_concurrent}\n"
                "\n"
                "If you surface a SpeculativeCandidate, EVERY candidate must "
                "have suggested_position_pct_of_net_worth <= the cap, AND "
                "risk_ceiling_check=true. Do NOT recommend candidates that "
                "would breach the cap. The orchestrator will silently drop "
                "any over-cap candidates anyway, so you save the user a "
                "confused glance by getting it right here.\n"
            )
            system = system + cap_block

        # User directive — authoritative input from the human captured on
        # this synthesis run. **Note**: in f8faaca this lived in the
        # system prompt, but synthesis #27 + #28 reproducibly hit empty
        # output from Opus via the bundled claude.exe SDK (4 retries
        # each, all returned ""). System prompts in Claude have
        # different parsing (prefix-caching, length heuristics) and
        # large variable content there appears to trigger the empty-
        # stream path. We instead include a short DIRECTIVE POINTER in
        # the system prompt (so the model knows to look for it) and
        # place the actual text in the user prompt below where Claude
        # tolerates variable content cleanly.
        if user_directive:
            system = system + (
                "\n\nUSER DIRECTIVE PRESENT: a USER DIRECTIVE block appears "
                "in the user message below. Treat it as authoritative human "
                "input. Where it states AGREED objections, bake them into "
                "the new draft and don't re-litigate. Where it states "
                "DISAGREED objections with a user counter-position, use "
                "the counter-position as the target — derive the targets / "
                "actions / numbers from it. Where it states DEFERRED, "
                "re-evaluate honestly. If the directive conflicts with "
                "hard data constraints (legal deadlines, mandate-coherence), "
                "surface the conflict prominently in the rationale rather "
                "than papering over either side.\n"
            )

        # T4.8a — render the prior-items index for the lineage contract.
        # Group by horizon so the model can scan one column at a time.
        prior_items_block: str
        if prior_items_index:
            by_horizon: dict[str, list[dict]] = {"long": [], "medium": [], "short": []}
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
                    label = it.get("label", "")
                    value = it.get("value", "")
                    unit = it.get("unit", "")
                    kind = it.get("item_kind", "")
                    iid = it.get("item_id", "?")
                    src = it.get("from_plan", "")
                    suffix = (
                        f"  (from plan #{src})" if src else ""
                    )
                    lines.append(
                        f"    - {iid}  ({kind})  label={label!r}"
                        f"  value={value} {unit}{suffix}"
                    )
            prior_items_block = "\n".join(lines) if lines else "  (none)"
        else:
            prior_items_block = "  (no prior items — first synthesis for this user)"

        # User directive lives at the TOP of the user prompt (when
        # present) so the model encounters it before the rest of the
        # context. Empty (default) omits the section entirely.
        directive_section: list[str] = []
        if user_directive:
            directive_section.append(
                "=== USER DIRECTIVE (authoritative human input on this run) ===\n"
                + user_directive
            )

        # Derived headline numbers (the deterministic resolver manifest) lead
        # the prompt body after the directive: these are the values the synth
        # MUST consume for every headline figure instead of authoring its own.
        if resolved_numbers_block:
            directive_section.append(
                "=== DERIVED HEADLINE NUMBERS (AUTHORITATIVE — USE VERBATIM) ===\n"
                + resolved_numbers_block
            )

        # Phase 1 of the integration plan removed the
        # ``=== PRIOR CURRENT PLAN ===`` block from this user prompt.
        # That block fed the model the previous draft's prose body and
        # was the proximate cause of revision-history leakage in v20
        # (synth dutifully wrote "retracts the prior framing" etc.).
        # The ID STABILITY contract in the system prompt + the PRIOR
        # ITEMS INDEX below provide everything the model needs to
        # preserve item_ids across revisions — without exposing the
        # prior prose to be paraphrased.
        usr = "\n\n".join(directive_section + [
            "================ HARD FACTS — GROUND TRUTH ================\n"
            "Derive EVERY target and rate from THESE + the goal. The DERIVED HEADLINE "
            "NUMBERS block above is authoritative; the holdings + analysis below are the "
            "current-state facts to reason from.",
            "=== PORTFOLIO SNAPSHOT (current holdings) ===\n" + portfolio_snapshot_summary,
            "=== ANALYST REPORTS (Phase 1 outputs) ===\n" + analyst_reports_text,
            "=== DEBATE OUTCOMES (Phase 2 outputs, one per horizon) ===\n" + debate_outcomes_text,
            "================ SOFT REFERENCE — HISTORY (NOT a source of targets) ========\n"
            "For continuity, narrative, and item_id lineage ONLY. Do NOT carry any "
            "number or target forward from this section — a target must be DERIVED from "
            "the HARD FACTS, never inherited from the past plan or past execution. The "
            "past sale cadence (e.g. a 3,000 sh/yr figure) is HISTORY, not a target. If "
            "you reference anything here, frame it as 'previously' / 'what was done', "
            "NEVER as 'the plan'.",
            "=== BASELINE PLAN [reference only] ===\n" + (baseline_distillate_md or "(no baseline)"),
            "=== PRIOR ITEMS INDEX [reference — item_id stability only] ===\n"
            + prior_items_block,
            "=== RECENT FILLS + DECISIONS — past execution, last 90 days [reference] ===\n"
            + recent_fills_summary,
            "Produce the PlanSynthesisOutput JSON now. Honor the medium-horizon "
            "centerpiece framing. If status=no_change for a horizon, deltas_from_prior "
            "must be empty AND the rationale must explicitly justify why nothing changed. "
            "Honor the item_id lineage contract — REUSE ids from the PRIOR ITEMS INDEX "
            "when revising; only mint new ids for genuinely new items.\n\n"
            # JSON-start discipline (codex tandem MUST-FIX). The SDK's
            # --json-schema enforcement is the primary guardrail; this
            # instruction is the prose backstop in case the model
            # tries to wrap output in a markdown fence or prefix
            # prose. Synth #58 truncated specifically at the fence
            # opener — telling the model not to emit one removes the
            # entire failure class.
            "RESPONSE FORMAT: emit the JSON object directly — no "
            "markdown code fences, no preamble, no commentary. Your "
            "response MUST start with `{` and END with `}`.",
        ])
        return system, usr


__all__ = ["PlanSynthesizerAgent"]
