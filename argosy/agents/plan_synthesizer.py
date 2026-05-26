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

from argosy.agents._plan_authority import AUTHORITY_DISCLAIMER
from argosy.agents.base import BaseAgent
from argosy.agents.plan_synthesizer_types import PlanSynthesisOutput


class PlanSynthesizerAgent(BaseAgent[PlanSynthesisOutput]):
    """Phase 3 of plan_synthesis_flow."""

    agent_role = "plan_synthesizer"
    output_model = PlanSynthesisOutput
    require_citations = True
    max_tokens = 16384

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
    ) -> tuple[str, str]:
        system = (
            "You are the plan synthesizer on the Argosy fleet — Phase 3 of the "
            "monthly synthesis flow.\n\n"
            f"{AUTHORITY_DISCLAIMER}\n\n"
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
            "ITEM_ID LINEAGE CONTRACT (T4.8a — load-bearing for the UI's "
            "history view): the user's UI shows how a target/action/theme "
            "EVOLVED across plan iterations using item_id as the join key.\n"
            "  - If you're REVISING something that existed in the prior "
            "    draft or prior current plan, KEEP THE SAME item_id. Look "
            "    at the PRIOR ITEMS INDEX block below; if any item matches "
            "    your concept (same horizon + same intent + same target "
            "    variable), reuse its exact item_id verbatim. Do NOT mint "
            "    a new id for what is conceptually the same target.\n"
            "  - If you're adding a GENUINELY NEW item that has no prior "
            "    counterpart, generate a stable kebab-case id following the "
            "    convention `<horizon>.<kind>.<slug>` (e.g. "
            "    `medium.targets.nvda_share_of_portfolio_12mo`) — choose a "
            "    slug that will survive future revisions (don't bake a "
            "    transient number like '2026' into the slug unless it's "
            "    truly anchored to that year).\n"
            "  - If a prior item should be DROPPED, emit a Delta with "
            "    change_kind='removed' using its original item_id; don't "
            "    just omit it silently.\n"
            "Reusing the id when intent is the same is the most common "
            "case. Missing this contract breaks the history chip on the "
            "/plan page and forces the user to manually match revisions "
            "across drafts.\n\n"
            "CITATIONS REQUIRED for every numeric or directional claim. Use "
            "the format `agent_report:<id>` for analyst evidence, "
            "`decision_run:<id>` for prior synthesis lineage, "
            "`domain_kb:<path>` for jurisdiction rules, "
            "`plan_section:<heading>` for baseline references, "
            "`prior_current:<id>` for diff context.\n\n"
            "OUTPUT must be a JSON object conforming to:\n"
            f"{PlanSynthesisOutput.model_json_schema()}\n"
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

        usr = "\n\n".join(directive_section + [
            "=== BASELINE DISTILLATE ===\n" + (baseline_distillate_md or "(no baseline)"),
            "=== PRIOR CURRENT PLAN ===\n" + (prior_current_md or "(no prior current — first synthesis)"),
            # T4.8a lineage payload — placed prominently AFTER the prior
            # plan markdown so the model has both the narrative + the
            # structured ids next to each other.
            "=== PRIOR ITEMS INDEX (T4.8a — preserve item_id across revisions) ===\n"
            + prior_items_block,
            "=== ANALYST REPORTS (Phase 1 outputs) ===\n" + analyst_reports_text,
            "=== DEBATE OUTCOMES (Phase 2 outputs, one per horizon) ===\n" + debate_outcomes_text,
            "=== PORTFOLIO SNAPSHOT ===\n" + portfolio_snapshot_summary,
            "=== RECENT FILLS + DECISIONS (last 90 days) ===\n" + recent_fills_summary,
            "Produce the PlanSynthesisOutput JSON now. Honor the medium-horizon "
            "centerpiece framing. If status=no_change for a horizon, deltas_from_prior "
            "must be empty AND the rationale must explicitly justify why nothing changed. "
            "Honor the item_id lineage contract — REUSE ids from the PRIOR ITEMS INDEX "
            "when revising; only mint new ids for genuinely new items.",
        ])
        return system, usr


__all__ = ["PlanSynthesizerAgent"]
