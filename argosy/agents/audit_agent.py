"""Audit agent (SDD §3.6, Phase 7).

Reviews last week's `agent_reports` rows; identifies systematic errors:
- One agent role consistently producing low-confidence outputs.
- A tier consistently rejected by the fund manager.
- A specific prompt pattern misfiring across decisions.

Output: `AuditReport` with `findings: list[Finding]`. Each finding has
`agent_role`, `pattern`, `evidence_run_ids: list[int]`, and
`proposed_prompt_tweak: str`. **Opus**.

Runs from a new `audit` cadence (weekly).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent, ConfidenceBand


class Finding(BaseModel):
    agent_role: str
    pattern: str = Field(
        description="Short description of the systematic issue identified."
    )
    evidence_run_ids: list[int] = Field(
        default_factory=list,
        description="agent_reports row IDs that exemplify the pattern.",
    )
    proposed_prompt_tweak: str = Field(
        default="",
        description="Concrete suggested change to the agent's system prompt.",
    )
    severity: str = Field(default="warning")


class AuditReport(BaseModel):
    findings: list[Finding] = Field(default_factory=list)
    summary: str = Field(default="")
    week_start: str = Field(default="", description="ISO date of week start.")
    week_end: str = Field(default="", description="ISO date of week end.")
    runs_reviewed: int = 0
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM
    cited_sources: list[str] = Field(
        default_factory=list,
        description="Audit log paths or agent_reports row references "
        "underpinning the findings.",
    )


class AuditAgent(BaseAgent[AuditReport]):
    """Opus-class audit agent. Weekly self-review of the fleet."""

    agent_role = "audit"
    output_model = AuditReport
    require_citations = True
    # max_tokens driven by DEFAULT_MAX_TOKENS_BY_ROLE (16000).

    def build_prompt(
        self,
        *,
        runs_summary: list[dict[str, Any]],
        week_start: str,
        week_end: str,
    ) -> tuple[str, str]:
        """Build the prompt.

        Args:
            runs_summary: list of `{id, agent_role, model, confidence,
                tokens_in, tokens_out, cost_usd, response_excerpt,
                fund_manager_decision}` dicts the caller has produced
                from joining `agent_reports` + `decision_runs`.
            week_start: ISO date.
            week_end: ISO date.
        """
        system = (
            "You are the audit agent on the Argosy fleet. You review the "
            "past week's agent runs and identify SYSTEMATIC patterns of "
            "error or weakness. You do NOT critique individual decisions; "
            "you look for repeating issues across runs.\n\n"
            "Patterns worth flagging:\n"
            "  - One agent role consistently producing LOW confidence.\n"
            "  - One agent role consistently being overruled by the fund "
            "manager (BLOCK rate >= 30% on its proposals).\n"
            "  - One model assignment being noticeably more expensive than "
            "its outputs justify.\n"
            "  - Prompts that produce outputs missing required citations "
            "more than once.\n\n"
            "Each finding MUST cite specific agent_reports row IDs in "
            "`evidence_run_ids` and a `cited_sources` reference (e.g., "
            "'agent_reports:42,43,44'). Findings without citations are "
            "treated as hallucinations.\n\n"
            "OUTPUT must be a JSON object conforming to this schema:\n"
            f"{AuditReport.model_json_schema()}\n"
        )

        rows: list[str] = []
        for r in runs_summary[:200]:  # cap context size
            rows.append(
                f"  - id={r.get('id')} role={r.get('agent_role')} "
                f"model={r.get('model')} conf={r.get('confidence')} "
                f"toks=({r.get('tokens_in')}/{r.get('tokens_out')}) "
                f"cost=${r.get('cost_usd')} "
                f"fm={r.get('fund_manager_decision', '')} "
                f"excerpt={(r.get('response_excerpt') or '')[:160]!r}"
            )
        rows_block = "\n".join(rows) or "  (no runs in window)"

        user = (
            f"Audit window: {week_start} → {week_end}\n"
            f"Runs reviewed: {len(runs_summary)}\n\n"
            "AGENT_REPORTS ROWS (compact):\n"
            f"{rows_block}\n\n"
            "Produce an AuditReport JSON now. Findings should be "
            "actionable — each `proposed_prompt_tweak` should be a "
            "concrete sentence the operator could paste into the "
            "agent's system prompt."
        )
        return system, user


__all__ = ["AuditAgent", "AuditReport", "Finding"]
