"""Shared authority disclaimer for plan-touching agents.

Per spec §6.1: every agent prompt that injects plan context must
include this disclaimer so the model cannot drift toward treating the
plan as authority.

Imported by:
  - argosy.agents.advisor (advisor turns)
  - argosy.agents.plan_synthesizer (synthesis Phase 3)
  - argosy.agents.plan_critique (when run as part of synthesis Phase 1)
  - all decision_flow agents that read the current plan

Do NOT modify this string lightly — wording was deliberately chosen.
If you must edit it, update tests/test_plan_authority.py too.
"""

from __future__ import annotations

AUTHORITY_DISCLAIMER = (
    "AUTHORITY NOTE — read carefully:\n\n"
    "The plan you have been provided is ONE INPUT among portfolio state, "
    "market data, news, and the analyst reports you receive. Cite it when "
    "you reason; DISAGREE when evidence warrants. The plan is NOT "
    "authority. Your job is to be loyal to the user, not to the plan. "
    "If the plan's stated targets or assumptions are stale, contradicted "
    "by current data, or no longer best-serving the user's goals, say so "
    "explicitly and cite the contradicting evidence."
)


__all__ = ["AUTHORITY_DISCLAIMER"]
