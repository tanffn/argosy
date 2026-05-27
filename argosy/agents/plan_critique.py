"""Plan-critique agent (SDD §3.1, Appendix B.6).

Inputs: plan markdown + portfolio snapshot + user_context + relevant
domain_knowledge files. Output: a structured `PlanCritiqueReport` with one
or more `Finding`s, each rated RED / YELLOW / GREEN with cited evidence.

The plan is INPUT, not authority. RED findings must cite specific
domain_knowledge files or specific portfolio numbers as evidence.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent, ConfidenceBand


class Finding(BaseModel):
    """One classified finding against a plan item."""

    plan_item_ref: str = Field(
        description="Short reference to the plan section/heading or specific rule, "
        "e.g., '§5.2 NVDA reduction schedule', 'Allocation Target — Growth 20%'."
    )
    severity: Literal["RED", "YELLOW", "GREEN"]
    topic: str = Field(
        description="Short topic label, e.g., 'Concentration Risk', 'Tax Treatment', "
        "'Estate Exposure', 'FX', 'Allocation Drift'."
    )
    summary: str = Field(
        description="One-sentence verdict on the finding."
    )
    evidence: list[str] = Field(
        default_factory=list,
        description="Concrete evidence: numbers from the portfolio snapshot, "
        "rule citations from domain_knowledge files, FX delta, etc. Each "
        "entry should be a complete sentence the user can verify.",
    )
    cited_sources: list[str] = Field(
        default_factory=list,
        description="domain_knowledge file paths or external URLs supporting "
        "the evidence. Required for RED and YELLOW findings; recommended "
        "for GREEN.",
    )
    recommended_action: str | None = Field(
        default=None,
        description="Optional concrete action for the user (do/don't); never "
        "auto-edit the plan, just propose.",
    )


class PlanCritiqueReport(BaseModel):
    """Top-level structured critique returned by the plan-critique agent."""

    plan_label: str = Field(
        description="Short identifier of the plan critiqued (e.g., 'Jacobs_Wealth_Plan v2.0').",
    )
    snapshot_label: str = Field(
        description="Short identifier of the portfolio snapshot used (e.g., 'TSV 26-May').",
    )
    findings: list[Finding] = Field(default_factory=list)
    overall_summary: str = Field(
        description="2-4 sentence executive summary, bias toward concrete numbers.",
    )
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM
    cited_sources: list[str] = Field(
        default_factory=list,
        description="Top-level list of every distinct source cited across all "
        "findings. Required to be non-empty.",
    )


# ----------------------------------------------------------------------
# Agent
# ----------------------------------------------------------------------


class PlanCritiqueAgent(BaseAgent[PlanCritiqueReport]):
    """Plan-critique analyst. Produces RED/YELLOW/GREEN findings on a plan."""

    agent_role = "plan_critique"
    output_model = PlanCritiqueReport
    require_citations = True
    # max_tokens driven by DEFAULT_MAX_TOKENS_BY_ROLE (32000).

    def build_prompt(
        self,
        *,
        plan_label: str,
        plan_markdown: str,
        snapshot_label: str,
        snapshot_summary: str,
        user_context_yaml: str,
        domain_kb_files: dict[str, str],
        recent_events: str = "",
        user_directive: str = "",
    ) -> tuple[str, str, list[tuple[str, str]]]:
        """Build the system+user prompt.

        Args:
            plan_label: a short identifier for the plan version.
            plan_markdown: the full plan document text (already trimmed if huge).
            snapshot_label: identifier for the portfolio snapshot.
            snapshot_summary: a compact, human-readable summary of the
                snapshot (positions, FX, concentration). Comes from the
                ingestion pipeline.
            user_context_yaml: serialized user_context (identity, goals,
                constraints) — same as what intake gathers.
            domain_kb_files: mapping `path -> file_contents` for the relevant
                domain_knowledge files. Pass at minimum the files in
                `domain_knowledge/tax/israel/`, `treaties/`, and
                `tax/us/` for an Israeli tenant.
            recent_events: optional free-text on recent material events
                (news flagged by the news analyst, FX moves, etc.). Wrapped
                in `<news>...</news>` automatically — content treated as
                data per cross-cutting rule.

        Wave A: returns ``(system, user, sources)``. The plan markdown,
        portfolio snapshot summary, and each ``domain_kb_files`` entry are
        extracted into Citations API document blocks (``plan/markdown``,
        ``portfolio/snapshot``, and one per domain_knowledge file keyed by
        its relative path) rather than inlined into the user prompt. The
        user prompt references each by ``source_id`` so the model's output
        carries character-offset citations back into the source text.
        ``user_context_yaml`` and ``recent_events`` stay inline (small,
        not authoritative sources for cited claims).
        """
        sources: list[tuple[str, str]] = []
        if plan_markdown.strip():
            sources.append(("plan/markdown", plan_markdown))
        if snapshot_summary.strip():
            sources.append(("portfolio/snapshot", snapshot_summary))
        sources.extend(
            (path, contents)
            for path, contents in sorted(domain_kb_files.items())
        )

        kb_refs = (
            ", ".join(path for path in sorted(domain_kb_files.keys()))
            if domain_kb_files
            else "(no domain_knowledge files were provided to this run)"
        )

        system = (
            "You are the plan-critique analyst on the Argosy fleet.\n\n"
            "The plan you are critiquing is INPUT, not authority. You may flag "
            "any item RED if data, math, or current rules disagree.\n\n"
            "For each plan item (rule, target, schedule, allocation), classify:\n"
            "  - GREEN: aligns with current data and rules.\n"
            "  - YELLOW: aligns but assumptions are aging or thin (cite which).\n"
            "  - RED: conflicts with current data, math, or rules (cite specifically).\n\n"
            "Do not soften RED findings. Do not auto-edit the plan. Cite every "
            "numeric or regulatory claim with a domain_knowledge file path or "
            "external source URL.\n\n"
            "The plan markdown is attached as document source `plan/markdown`. "
            "The portfolio snapshot summary is attached as `portfolio/snapshot`. "
            "Relevant domain_knowledge files are attached one per file, titled "
            "by their relative path. Reference these source_ids in "
            "`cited_sources` for every claim that reads from them.\n\n"
            "OUTPUT must be a JSON object conforming to this schema:\n"
            f"{PlanCritiqueReport.model_json_schema()}\n"
        )

        # User directive — authoritative input from the human captured on
        # this synthesis run. Same pattern as plan_synthesizer.py /
        # fund_manager.py (post-a5d317c): a short DIRECTIVE POINTER lives
        # in the SYSTEM prompt; the verbatim directive content lives at
        # the TOP of the USER prompt below. Variable content in system
        # prompts has reproducibly triggered the bundled claude.exe SDK's
        # empty-output path (synthesis #27/#28).
        if user_directive:
            system = system + (
                "\nUSER DIRECTIVE PRESENT: a USER DIRECTIVE block appears in the "
                "user message below capturing the human's per-objection stances "
                "from the prior round. Respect the user's resolved positions:\n"
                "  - Where the user has AGREED a critique finding is resolved, "
                "do NOT re-raise it on this run.\n"
                "  - Where the user has DISAGREED with a prior finding and "
                "supplied a counter-position, treat the counter-position as "
                "authoritative; only re-raise the original finding if the data "
                "still contradicts the counter-position.\n"
                "  - Where the user has DEFERRED, evaluate freshly.\n"
                "You retain authority to raise NEW findings on items the user "
                "has not addressed.\n"
            )

        plan_ref = (
            "PLAN MARKDOWN: see document `plan/markdown`."
            if plan_markdown.strip()
            else "PLAN MARKDOWN: (no plan markdown supplied)"
        )
        snapshot_ref = (
            "PORTFOLIO SNAPSHOT SUMMARY: see document `portfolio/snapshot`."
            if snapshot_summary.strip()
            else "PORTFOLIO SNAPSHOT SUMMARY: (no snapshot summary supplied)"
        )

        user_parts: list[str] = []
        # User directive lives at the TOP of the user prompt (when
        # present) so the model encounters it before the rest of the
        # context. Empty (default) omits the section entirely so the
        # byte-identity invariant on the happy path holds.
        if user_directive:
            user_parts.append(
                "=== USER DIRECTIVE (authoritative human input on this run) ===\n"
                + user_directive
            )
        user_parts.append(f"PLAN LABEL: {plan_label}")
        user_parts.append(f"SNAPSHOT LABEL: {snapshot_label}")
        user_parts.append("=== USER CONTEXT (YAML) ===\n```yaml\n" + user_context_yaml + "\n```")
        user_parts.append(snapshot_ref)
        user_parts.append(
            "=== RELEVANT DOMAIN KNOWLEDGE ===\n"
            "The relevant domain_knowledge files are attached as document "
            "sources (one per file). Cite them by their relative path "
            "(e.g. `domain_knowledge/tax/israel/section_102.md`).\n"
            f"Attached domain_knowledge sources: {kb_refs}"
        )
        if recent_events.strip():
            user_parts.append("=== RECENT EVENTS ===\n<news>\n" + recent_events + "\n</news>")
        user_parts.append(plan_ref)
        user_parts.append(
            "Produce the PlanCritiqueReport JSON now. Make findings concrete; "
            "anchor each one to specific numbers in the snapshot or specific "
            "lines/rules in the cited domain_knowledge files."
        )

        return system, "\n\n".join(user_parts), sources


__all__ = ["Finding", "PlanCritiqueAgent", "PlanCritiqueReport"]
