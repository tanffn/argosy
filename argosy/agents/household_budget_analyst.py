"""HouseholdBudgetAnalystAgent — synthesis Phase 1 #10 (SDD §6.11 + §18).

Feeds household cash-flow context into plan synthesis. Without this the
synthesizer has no view on monthly burn, income runway, or safe-
withdrawal headroom — it can recommend "harvest RSU on vest" without
knowing whether the user even needs the liquidity (and conversely it
can recommend "DCA aggressively" without checking the cushion against
monthly expenses).

Inputs (assembled by ``argosy.orchestrator.flows.plan_synthesis.inputs``
into a new ``household_budget_payload`` field):

* ``monthly_burn_nis``         — `identity.monthly_expenses_total_nis`
* ``monthly_burn_window``      — e.g. "12-month rolling" / "Apr 2026"
* ``income_streams``           — list[{source, monthly_nis, note}]
* ``liquid_assets_usd_k``      — sum of liquid positions (excl. NVDA RSU pool)
* ``safe_withdrawal_monthly``  — 4% rule × liquid / 12
* ``rsu_annual_usd``           — from identity.rsu_annual_usd
* ``emergency_fund_months``    — identity.emergency_fund_months
* ``fx_usd_nis``               — for cross-currency comparison
* ``runway_assessment``        — sentinel: "deficit" / "tight" / "comfortable"

Output: ``HouseholdBudgetReport`` consumed by the synthesizer at Phase 3.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent, ConfidenceBand


class HouseholdBudgetReport(BaseModel):
    runway_class: Literal["deficit", "tight", "comfortable", "abundant"] = (
        "comfortable"
    )
    monthly_burn_nis: float = 0.0
    monthly_income_nis: float = 0.0
    monthly_net_nis: float = 0.0
    safe_withdrawal_monthly_usd: float = 0.0
    headroom_summary: str = Field(
        default="",
        description=(
            "Two-sentence narrative: does the plan have liquidity headroom "
            "to absorb a market downturn, taxes, or unplanned spend? "
            "Cite specific numbers from the snapshot."
        ),
    )
    key_concerns: list[str] = Field(
        default_factory=list,
        description=(
            "Short bullets — concrete risks the synthesizer should "
            "weigh (e.g. 'monthly burn exceeds salary net by 8% — "
            "household runs deficit absent RSU/ESPP liquidation')."
        ),
    )
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM
    cited_sources: list[str] = Field(
        default_factory=list,
        description=(
            "Source ids backing the report: typically "
            "`household_budget/identity_yaml`, "
            "`household_budget/portfolio_snapshot`, and any "
            "income-stream-specific identifiers."
        ),
    )


class HouseholdBudgetAnalystAgent(BaseAgent[HouseholdBudgetReport]):
    """Phase 1 analyst — household cash-flow + safe-withdrawal context.

    Reads the assembled ``household_budget_payload`` produced by the
    synthesis input assembler. Tolerates partial data: when income or
    burn figures are missing, downgrades confidence to LOW and surfaces
    the gap in ``key_concerns`` rather than fabricating a number.
    """

    agent_role = "household_budget"
    output_model = HouseholdBudgetReport
    require_citations = True
    max_tokens = 2048

    def build_prompt(
        self,
        *,
        household_budget_payload: dict[str, Any],
    ) -> tuple[str, str, list[tuple[str, str]]]:
        """Build prompt from the household_budget_payload dict.

        Required keys (any can be missing — surface in concerns):

        * monthly_burn_nis, monthly_burn_window
        * income_streams (list of {source, monthly_nis, note})
        * liquid_assets_usd_k, safe_withdrawal_monthly_usd
        * rsu_annual_usd, emergency_fund_months
        * fx_usd_nis
        """
        payload = household_budget_payload or {}

        system = (
            "You are the household-budget analyst on the Argosy fleet. "
            "Your job: assess the household's monthly cash-flow position "
            "and surface any liquidity / runway concerns the synthesizer "
            "should weigh when proposing actions.\n\n"
            "Rules:\n"
            "  - Cite source ids for every numeric claim. The household "
            "snapshot is attached as a single document block titled "
            "`household_budget/identity_yaml`; use that source_id (plus "
            "any per-stream ones you see) in `cited_sources`.\n"
            "  - Do NOT predict the future or recommend trades. Your job "
            "is to describe the cash-flow STATE so the synthesizer can "
            "react.\n"
            "  - When monthly_burn or income data is missing, downgrade "
            "confidence to LOW and surface the gap explicitly in "
            "`key_concerns` rather than fabricating a number.\n"
            "  - runway_class taxonomy:\n"
            "      deficit — burn > income; depends on RSU/ESPP "
            "liquidation each month to stay flat\n"
            "      tight — net positive but emergency_fund_months < 3 OR "
            "safe_withdrawal_monthly < monthly_burn\n"
            "      comfortable — net positive AND emergency_fund_months "
            ">= 6 AND safe_withdrawal_monthly >= monthly_burn\n"
            "      abundant — net positive AND emergency_fund_months >= "
            "12 AND safe_withdrawal_monthly >= 2x monthly_burn\n\n"
            "OUTPUT must be a JSON object conforming to this schema:\n"
            f"{HouseholdBudgetReport.model_json_schema()}\n"
        )

        sources: list[tuple[str, str]] = [
            ("household_budget/identity_yaml", str(payload))
        ]

        user_lines = [
            "HOUSEHOLD BUDGET SNAPSHOT:",
            f"  monthly_burn_nis: {payload.get('monthly_burn_nis', '?')}",
            f"  monthly_burn_window: {payload.get('monthly_burn_window', '?')}",
            f"  rsu_annual_usd: {payload.get('rsu_annual_usd', '?')}",
            f"  liquid_assets_usd_k: {payload.get('liquid_assets_usd_k', '?')}",
            f"  safe_withdrawal_monthly_usd: {payload.get('safe_withdrawal_monthly_usd', '?')}",
            f"  emergency_fund_months: {payload.get('emergency_fund_months', '?')}",
            f"  fx_usd_nis: {payload.get('fx_usd_nis', '?')}",
        ]
        streams = payload.get("income_streams") or []
        if streams:
            user_lines.append("  income_streams:")
            for s in streams:
                if not isinstance(s, dict):
                    continue
                src = s.get("source", "?")
                amt = s.get("monthly_nis", "?")
                note = s.get("note", "")
                user_lines.append(
                    f"    - {src}: {amt} NIS/month"
                    + (f"  ({note})" if note else "")
                )
        else:
            user_lines.append("  income_streams: (none catalogued)")

        user_lines.append(
            "\nProduce a HouseholdBudgetReport JSON now. Cite "
            "`household_budget/identity_yaml` (and any per-stream "
            "source_ids you derive) in cited_sources."
        )
        user = "\n".join(user_lines)
        return system, user, sources


__all__ = ["HouseholdBudgetAnalystAgent", "HouseholdBudgetReport"]
