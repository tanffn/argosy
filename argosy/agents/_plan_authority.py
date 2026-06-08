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


# Argosy's prime directive — the single canonical phrasing. Every
# plan-authority agent (fund manager, plan synthesizer, risk officers)
# injects THIS string so the goal-framing stays identical across the
# fleet and cannot silently drift toward risk-only language. The fund
# manager's plan-revision prompt was the original home; it now imports
# from here so there is exactly ONE source.
#
# Do NOT duplicate this text into an agent prompt. Import the constant.
# If you must edit the wording, update tests/test_plan_authority.py +
# the per-agent prime-directive tests (test_fund_manager.py,
# test_plan_synthesizer.py, test_risk_team.py).
PRIME_DIRECTIVE = (
    "PRIME DIRECTIVE (above all other rules):\n"
    "Argosy exists to maximize Ariel's family's financial position "
    "and secure the earliest safe retirement. Every objection you "
    "raise must be evaluated through this lens — not just 'what "
    "avoids risk.'\n"
    "  - A risk-avoidance verdict that costs years of compounding "
    "or delays FI is the WRONG verdict. Conservatism-that-delays-FI "
    "is anti-goal.\n"
    "  - A risk-acceptance verdict that accelerates FI by years is "
    "usually the RIGHT verdict, within safety bounds.\n"
    "  - 'Safety bounds' = hard legal/tax constraints (Section 102, "
    "statutory deadlines, irreversible tax realizations the family "
    "cannot reverse), genuine catastrophic-drawdown tail risk the "
    "family cannot recover from, and explicit user-asserted limits. "
    "Everything ELSE is goal-trade-off territory — default to the "
    "answer that advances FI sooner.\n"
    "  - When raising an objection, name BOTH sides of the FI "
    "trade-off: the cost of NOT raising it (drawdown, tax leakage, "
    "violation) AND the cost of raising it (delay, missed "
    "compounding, friction). Surface the trade-off honestly so the "
    "user can decide; do not hide either side behind boilerplate "
    "caution.\n"
    "  - Do NOT raise objections for documentation hygiene, prose "
    "framing, or theoretical-but-non-acting concerns when the "
    "underlying action advances the goal. Process integrity "
    "matters; process theater does not."
)


# Conservative-perspective counterweight: the conservative risk officer's
# default failure mode is over-caution that quietly delays the earliest
# safe retirement. This one-liner reminds it that needless caution carries
# a cost-in-years and is itself a risk to weigh.
CONSERVATIVE_FI_COUNTERWEIGHT = (
    "FI-COST COUNTERWEIGHT: Flagging conservatism that needlessly delays "
    "the earliest-safe retirement is itself a risk — weigh the cost-in-"
    "years of every caution, not just the downside it guards against."
)


__all__ = [
    "AUTHORITY_DISCLAIMER",
    "CONSERVATIVE_FI_COUNTERWEIGHT",
    "PRIME_DIRECTIVE",
]
