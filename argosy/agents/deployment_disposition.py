"""Deployment-disposition agent — the fleet answers "what should I DO with this
cash?", not just "approve/veto this line".

The deterministic funnel surfaces facts and the per-candidate fleet pass can veto
concentration-worsening buys — but that can leave cash with no on-plan home, which
must NEVER fall through as silent residue. This agent (fund-manager lens) produces
an AFFIRMATIVE disposition of the FULL deployable amount: every dollar is either
deployed into a specific sleeve/instrument, held as cash WITH A STATED REASON
(macro / valuations / awaiting deconcentration), or routed to a concrete action
(trim NVDA to deconcentrate, or raise a plan change to add a missing sleeve).
Holding cash is a valid answer ONLY when the fleet decides it for a reason.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from argosy.agents._plan_authority import PRIME_DIRECTIVE
from argosy.agents.base import BaseAgent, ConfidenceBand


class DispositionItem(BaseModel):
    action: Literal["deploy", "hold_cash", "deconcentrate_first", "raise_plan_change"]
    target: str = Field(
        description="Ticker/sleeve to deploy into; 'cash' for hold_cash; 'NVDA' for "
        "deconcentrate_first; the asset-class name for raise_plan_change."
    )
    amount_usd: float
    reason: str = Field(description="Why this dollar goes here — specific, cited if possible.")


class DeploymentDisposition(BaseModel):
    """An affirmative disposition of the FULL deployable cash. ``items`` must sum
    (within rounding) to the deployable amount — no dollar left unexplained."""

    summary: str = Field(description="One-paragraph recommendation for the whole amount.")
    items: list[DispositionItem]
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM
    cited_sources: list[str] = Field(default_factory=list)


class DeploymentDispositionAgent(BaseAgent[DeploymentDisposition]):
    """Fund-manager lens: recommend what to actually DO with the cash."""

    agent_role = "fund_manager"  # reuse the FM role's Opus default + settings
    output_model = DeploymentDisposition
    require_citations = False  # a recommendation, not a gated plan artifact

    def build_prompt(
        self,
        *,
        deployable_usd: float,
        book_nvda_pct: float,
        nvda_cap_pct: float,
        plan_instruments: list[dict],
        already_deploying: list[dict],
        blocked: list[dict],
        reserve_funded: bool,
        user_constraints: str = "",
    ) -> tuple[str, str]:
        system = (
            "You are the fund manager on the Argosy fleet, advising a single "
            "long-hold investor. Your job here is to RECOMMEND WHAT TO DO with a "
            "specific amount of deployable cash — an affirmative disposition, not a "
            "veto list.\n\n"
            f"{PRIME_DIRECTIVE}\n\n"
            "HARD RULES:\n"
            "  - Account for the FULL deployable amount. Every dollar is either "
            "deployed, held as cash, sent to deconcentration (trim NVDA), or gated "
            "behind a plan change — with a reason for each.\n"
            "  - A `deploy` item MUST name a real TICKER from the PLAN INSTRUMENTS "
            "menu below (e.g. deploy into EIMI, not 'emerging markets'). Do NOT "
            "invent instruments or use bare asset-class labels. Size deploys toward "
            "the plan's UNDER-TARGET sleeves — redirect the cash from the blocked, "
            "NVDA-dense buys into the plan's own non-NVDA sleeves (their tickers).\n"
            "  - Do NOT introduce an asset class that is ABSENT from the plan as a "
            "`deploy`. The current plan's sleeves are deliberate (it already carries "
            "a diversifier — Real assets/REIT/TIPS — and its authors chose the "
            "sleeve set). If you believe a genuinely-missing sleeve (e.g. gold / "
            "broad commodities) is warranted, emit it as `raise_plan_change` — a "
            "REQUEST routed to plan synthesis, NOT a buy — and JUSTIFY why it is "
            "additive versus the plan's EXISTING diversifiers rather than redundant. "
            "Do not assume it belongs.\n"
            "  - Holding cash is VALID only with an explicit reason (valuations, "
            "macro, or 'after trimming NVDA'). NEVER idle cash as a default/error.\n"
            "  - Deconcentration of an over-cap single name happens by SELLING it. "
            "If the real move is to trim NVDA, say so (deconcentrate_first) with an "
            "amount and note it is tax-sensitive (domain/tax agent confirms sizing).\n"
            "  - Prefer putting money to work over idle cash. Idle cash is an "
            "anti-goal.\n\n"
            "OUTPUT must be a JSON object conforming to this schema:\n"
            f"{DeploymentDisposition.model_json_schema()}\n"
        )
        menu_lines = "\n".join(
            f"  - {m.get('sleeve')}: target {m.get('target_pct')}% -> "
            f"tickers {m.get('tickers')}"
            for m in plan_instruments
        )
        user = (
            f"DEPLOYABLE CASH: ${deployable_usd:,.0f}\n\n"
            f"CONCENTRATION: the book is {book_nvda_pct:.0f}% NVDA (look-through) "
            f"vs a {nvda_cap_pct:.0f}% single-name cap. Reserve/T-bills: "
            f"{'already funded' if reserve_funded else 'still short of target'}.\n\n"
            f"PLAN INSTRUMENTS — the ONLY tickers you may deploy into (sleeve -> "
            f"target weight -> ticker(s)); size toward under-target sleeves:\n"
            f"{menu_lines}\n\n"
            f"ALREADY DEPLOYING (clean plan-fills the engine will execute): "
            f"{already_deploying}\n\n"
            f"BLOCKED / PENDING (NVDA-dense or reserve-overfill buys the engine "
            f"flagged — REDIRECT this cash into the plan's non-NVDA sleeves above): "
            f"{blocked}\n\n"
            f"USER CONSTRAINTS: {user_constraints or '(none supplied)'}\n\n"
            "Produce the DeploymentDisposition JSON now. Every `deploy` target is a "
            "ticker from the menu; items sum to the full deployable amount."
        )
        return system, user
