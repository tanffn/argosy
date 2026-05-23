"""Tax analyst agent (SDD §3.1, Phase 7).

Inputs: lots (per-ticker tax lots), recent fills, dividend payments,
RSU vest schedule, year-end planning context, plus a `domain_kb_files`
dict mirroring the plan-critique pattern. Output: `TaxReport` with
TLH candidates, dividend tax projections, RSU vest tax estimate, and
year-end planning hints. **Sonnet**.

Cite-every-claim discipline: this agent's output MUST cite a
`domain_knowledge/tax/...` file path for any rate or rule. The
boilerplate already enforces it; the prompt re-states it for emphasis.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent, ConfidenceBand


class TLHCandidate(BaseModel):
    ticker: str
    lot_id: str
    quantity: float
    cost_basis_usd: float
    current_price_usd: float
    unrealized_loss_usd: float
    wash_sale_risk: bool = False
    note: str = ""
    cited_sources: list[str] = Field(default_factory=list)


class DividendTaxProjection(BaseModel):
    ticker: str
    annual_dividend_usd: float
    estimated_withholding_usd: float
    estimated_residual_tax_usd: float
    cited_sources: list[str] = Field(default_factory=list)


class RsuVestEstimate(BaseModel):
    vest_date: str  # ISO date
    shares: float
    estimated_market_value_usd: float
    estimated_tax_usd: float
    cited_sources: list[str] = Field(default_factory=list)


class TaxReport(BaseModel):
    tlh_candidates: list[TLHCandidate] = Field(default_factory=list)
    dividend_projections: list[DividendTaxProjection] = Field(default_factory=list)
    rsu_vest_estimates: list[RsuVestEstimate] = Field(default_factory=list)
    year_end_hints: list[str] = Field(
        default_factory=list,
        description="Concrete year-end actions to consider (TLH harvest, "
        "Section 102 elections, charitable distribution timing, etc.).",
    )
    summary: str = Field(default="")
    confidence: ConfidenceBand = ConfidenceBand.MEDIUM
    cited_sources: list[str] = Field(
        default_factory=list,
        description="Top-level distinct domain_knowledge / external citations.",
    )


class TaxAnalystAgent(BaseAgent[TaxReport]):
    """Sonnet-class tax analyst.

    Cite-every-claim discipline against `domain_knowledge/tax/*` files
    is mandatory: every TLH candidate, dividend projection, and RSU
    estimate must cite a file path. The base-class citation gate
    enforces non-empty `cited_sources` at top level.
    """

    agent_role = "tax"
    output_model = TaxReport
    require_citations = True
    max_tokens = 4096

    def build_prompt(
        self,
        *,
        lots_summary: str,
        dividends_summary: str,
        rsu_schedule_summary: str,
        domain_kb_files: dict[str, str],
        user_context_yaml: str = "",
        recent_fills_summary: str = "",
    ) -> tuple[str, str, list[tuple[str, str]]]:
        """Build the prompt.

        Args:
            lots_summary: human-readable list of tax lots from `lots`.
            dividends_summary: recent + projected dividend payments per
                ticker.
            rsu_schedule_summary: upcoming RSU vest events.
            domain_kb_files: mapping `path -> file_contents` for the
                relevant `domain_knowledge/tax/...` files. Mandatory
                input — citation-gate fails without these.
            user_context_yaml: optional serialized user_context.
            recent_fills_summary: optional recent realized-gain context
                that informs wash-sale checks.

        Returns:
            ``(system, user, sources)``. Each ``domain_kb_files`` entry
            becomes a document source whose ``source_id`` is the file's
            relative path (e.g. ``domain_knowledge/tax/israel/capital_gains.md``)
            so the Citations API can attribute every rate/rule claim back
            to its authorising file. The bodies are no longer inlined in
            the user prompt; the user prompt references them by id.
        """
        # Each domain_knowledge/tax/... file becomes one document source,
        # keyed by its relative path so cited_sources strings line up
        # with the Citations API's document_title spans.
        sources: list[tuple[str, str]] = [
            (path, contents)
            for path, contents in sorted(domain_kb_files.items())
        ]
        ref_list = (
            ", ".join(sid for sid, _ in sources) if sources else "(none)"
        )

        system = (
            "You are the tax analyst on the Argosy fleet. You produce a "
            "structured tax report covering TLH candidates, dividend tax "
            "projections, RSU vest tax estimates, and year-end planning "
            "hints.\n\n"
            "RULES (mandatory):\n"
            "  - Every rate/rule claim MUST cite a `domain_knowledge/tax/...` "
            "file path. The relevant files are attached as document sources "
            "titled with their relative path; reference them by that path. "
            "Claims without a citation will be rejected.\n"
            "  - Wash-sale check: a TLH candidate flagged `wash_sale_risk=True` "
            "if a buy of the same ticker landed within 30 days, OR a buy is "
            "planned within 30 days. When in doubt, flag True.\n"
            "  - For Israeli-resident dividend tax projections, cite "
            "`domain_knowledge/tax/israel/...` AND `domain_knowledge/treaties/...` "
            "when the underlying security is US-listed.\n"
            "  - For RSU estimates, separate the at-vest income tax from the "
            "subsequent capital-gains tax on disposal.\n"
            "  - If no `domain_knowledge/tax/...` sources are attached, set "
            "confidence=LOW and do not fabricate rates.\n\n"
            "OUTPUT must be a JSON object conforming to this schema:\n"
            f"{TaxReport.model_json_schema()}\n"
        )

        user_parts: list[str] = []
        if user_context_yaml.strip():
            user_parts.append(
                "=== USER CONTEXT (YAML) ===\n```yaml\n" + user_context_yaml + "\n```"
            )
        user_parts.append("=== TAX LOTS ===\n" + (lots_summary or "(no lots)"))
        user_parts.append(
            "=== DIVIDENDS ===\n" + (dividends_summary or "(no dividend data)")
        )
        user_parts.append(
            "=== RSU SCHEDULE ===\n" + (rsu_schedule_summary or "(no upcoming vests)")
        )
        if recent_fills_summary.strip():
            user_parts.append("=== RECENT FILLS ===\n" + recent_fills_summary)
        user_parts.append(
            "=== DOMAIN KNOWLEDGE — TAX ===\n"
            "The relevant tax / treaty files are attached as document "
            "sources (one per file). Cite them by their relative path "
            f"(e.g. `domain_knowledge/tax/israel/capital_gains.md`).\n"
            f"Attached tax sources: {ref_list}"
        )
        user_parts.append(
            "Produce a TaxReport JSON now. Cite a `domain_knowledge/tax/...` "
            "file path for every claim. If insufficient data is available "
            "for a section, return an empty list there and explain in `summary`."
        )
        return system, "\n\n".join(user_parts), sources


__all__ = [
    "DividendTaxProjection",
    "RsuVestEstimate",
    "TLHCandidate",
    "TaxAnalystAgent",
    "TaxReport",
]
