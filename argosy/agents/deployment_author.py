"""DeploymentAuthorAgent — the fleet AUTHORS the allocation (the pivot's core).

A plain LLM prompt produced a better $180k allocation than Argosy's deterministic
water-fill because it reasoned holistically in one pass: "FWRA is ~62% US so it's
not real ex-US diversification; don't add US to a 60%-NVDA book; reserve for the
coming NVDA-sale CGT." This agent is that reasoner, made a first-class citizen: it
takes the decision packet (holdings + deployable cash + plan menu + sourced
instrument look-through + concentration + tax + policy signals) and emits ONE
``AllocationProposal``. It never runs deterministic math to place cash — that was the
old, beaten design. The deterministic verifier gates what it authors and, on a
fixable failure, bounces the machine-readable reasons back here to revise.

Output is the compact ``AllocationProposal`` schema directly (schema-constrained),
NOT a debate transcript. One call — not a RiskOfficer×3 fleet (that timed out and is
why the fleet was unusable). Reliability (hard timeout / process-tree kill / circuit
breaker / backend) is the wrapper's job, not this class's.
"""
from __future__ import annotations

from typing import Any

from argosy.agents._plan_authority import PRIME_DIRECTIVE
from argosy.agents.base import BaseAgent
from argosy.services.allocation_author.proposal import AllocationProposal


class DeploymentAuthorAgent(BaseAgent[AllocationProposal]):
    """Authors the AllocationProposal for a deploy request in one holistic pass."""

    agent_role = "deployment_author"
    output_model = AllocationProposal
    require_citations = False  # an authored decision, gated by the verifier, not a cited artifact
    use_structured_output = True  # emit the compact AllocationProposal JSON directly

    def build_prompt(
        self,
        *,
        packet: dict[str, Any],
        feedback: list | None = None,
    ) -> tuple[str, str]:
        system = (
            "You are the deployment author on the Argosy fleet — the single mind "
            "that decides what to DO with a specific amount of deployable cash for a "
            "long-hold, Israeli-resident (non-US-person) investor. You author the "
            "WHOLE move in one holistic pass, the way an expert advisor would.\n\n"
            f"{PRIME_DIRECTIVE}\n\n"
            "HOW TO REASON (this is why judgment beats a spreadsheet):\n"
            "  - LOOK-THROUGH, not labels. An all-world / global fund is US-HEAVY "
            "(e.g. FWRA is ~62% US) — it is NOT ex-US diversification. Use the "
            "SOURCED INSTRUMENT FACTS below; never treat a US-heavy fund as ex-US.\n"
            "  - CONCENTRATION. If the book is already at/over the NVDA cap, do NOT "
            "add US-equity or NVDA-correlated exposure on top — that deepens the "
            "problem. Direct fresh cash to genuinely diversifying, low-NVDA sleeves.\n"
            "  - TAX RESERVE. If a capital-gains-tax (CGT) liability is pending "
            "(e.g. from a coming NVDA sale), hold cash back for it FIRST via "
            "`cash_reserved_for_tax` — do not deploy money you owe the tax authority.\n"
            "  - DOMICILE / ESTATE. Prefer Irish UCITS (non-US-situs) instruments; "
            "the only sanctioned US-situs name is NVDA. Avoid opening new US-situs "
            "estate exposure.\n\n"
            "HARD RULES:\n"
            "  - Conservation: sum(buys.amount_usd) MUST equal `cash_to_deploy`, and "
            "`cash_to_deploy` + `cash_to_reserve` + `cash_reserved_for_tax` MUST "
            "equal the deployable amount. Account for every dollar.\n"
            "  - Only BUY a real ticker from the PLAN MENU below (or top up a current "
            "holding). Do NOT invent instruments or use bare asset-class labels.\n"
            "  - For EVERY buy, set `claimed_us_weight` (0..1) — your honest estimate "
            "of that instrument's US-equity weight. It is cross-checked against the "
            "sourced facts; a buy you call ex-US that is actually US-heavy is rejected.\n"
            "  - A sell may not exceed the held value of that symbol.\n"
            "  - Holding cash is valid ONLY with a stated reason in the rationale "
            "(valuations / macro / awaiting deconcentration) — never idle residue.\n\n"
            "OUTPUT: a single JSON object conforming to the AllocationProposal schema. "
            "No prose outside the JSON."
        )

        p = packet
        nvda = p.get("nvda") or {}
        reserve = p.get("reserve") or {}
        menu_lines = "\n".join(
            f"  - {m.get('sleeve')}: target {m.get('target_pct')}% -> "
            f"tickers {m.get('tickers')} (domicile {m.get('domiciles')})"
            for m in (p.get("plan_menu") or [])
        ) or "  (none)"
        facts_lines = "\n".join(
            f"  - {f.get('symbol')}: {f.get('us_weight', 0) * 100:.0f}% US "
            f"({f.get('source')}, {f.get('confidence')})"
            for f in (p.get("instrument_facts") or [])
        ) or "  (none)"
        holdings_lines = "\n".join(
            f"  - {sym}: ${val:,.0f}"
            for sym, val in sorted(
                (p.get("holdings") or {}).items(), key=lambda kv: -kv[1]
            )
        ) or "  (none)"

        cgt = float(p.get("cgt_liability_usd") or 0.0)
        cgt_line = (
            f"PENDING TAX LIABILITY: ~${cgt:,.0f} of CGT is coming (reserve it via "
            f"`cash_reserved_for_tax` before deploying)."
            if cgt > 0 else
            "PENDING TAX LIABILITY: none."
        )

        user = (
            f"DEPLOYABLE CASH: ${float(p.get('deployable_usd') or 0.0):,.0f}\n\n"
            f"{cgt_line}\n\n"
            f"CONCENTRATION: the book is {nvda.get('pct', 0)}% NVDA (look-through, "
            f"${nvda.get('lookthrough_usd', 0):,.0f} of ${nvda.get('book_usd', 0):,.0f}) "
            f"vs a {nvda.get('cap_pct', 0)}% single-name cap. "
            f"{'AT/OVER the cap — do not add US/NVDA-correlated exposure.' if nvda.get('pct', 0) >= nvda.get('cap_pct', 100) else 'Under the cap.'}\n\n"
            f"RESERVE: target ${reserve.get('target_usd', 0):,.0f}, current "
            f"${reserve.get('current_usd', 0):,.0f}, shortfall "
            f"${reserve.get('shortfall_usd', 0):,.0f}.\n\n"
            f"PLAN MENU — the tickers you may BUY (sleeve -> target -> tickers -> domicile):\n"
            f"{menu_lines}\n\n"
            f"SOURCED INSTRUMENT FACTS (US-equity look-through — trust THESE over labels):\n"
            f"{facts_lines}\n\n"
            f"CURRENT HOLDINGS (USD):\n{holdings_lines}\n\n"
            f"POLICY SIGNALS: {p.get('policy_signals') or '(none)'}\n\n"
            f"USER CONSTRAINTS: {p.get('user_constraints') or '(none)'}\n\n"
            + self._feedback_block(feedback)
            + "Author the AllocationProposal JSON now. Every buy is a plan-menu ticker "
            "(or a top-up of a current holding) with an honest `claimed_us_weight`; "
            "reserve the pending tax first; account for every dollar."
        )
        return system, user

    @staticmethod
    def _feedback_block(feedback: list | None) -> str:
        if not feedback:
            return ""
        lines = []
        for f in feedback:
            code = getattr(f, "code", "")
            detail = getattr(f, "detail", str(f))
            lines.append(f"  - [{code}] {detail}")
        return (
            "YOUR PREVIOUS PROPOSAL FAILED THE DETERMINISTIC VERIFIER. Revise it to "
            "correct EXACTLY these problems and re-author — keep everything that was "
            "fine:\n"
            + "\n".join(lines)
            + "\n\n"
        )


__all__ = ["DeploymentAuthorAgent"]
