"""Plan-change team — the fleet AUTHORS plan-content changes, verified by blind re-derivation.

Two judgment decisions for the living plan, each made by a REAL agent and
verified by ANOTHER agent that re-derives independently from the same RAW
facts (never seeing the author's reasoning) — divergence is compared IN CODE
and surfaced, never auto-resolved (feedback_adversarial_review_must_re_derive_blind):

  1. **Sleeve-instrument selection** (e.g. replace R1GR — the ~14%-NVDA
     Russell 1000 Growth UCITS — as the US-growth sleeve primary): the author
     picks ONE instrument from a sourced candidate table; a blind reviewer
     re-derives its own pick from the same table.
  2. **Diversifier-sleeve adjudication** (gold ON TRIAL vs growth-bearing
     diversifiers): burden of proof on gold; criterion is the PRIME DIRECTIVE
     (earliest safe retirement on the household's ACTUAL book), not
     volatility-damping for its own sake.

Determinism stays out of the judgment: the arithmetic floor (targets sum,
single-name cap, estate/domicile) is enforced downstream by the plan risk
kernel + domicile guardrail on the staged draft.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from argosy.agents._plan_authority import PRIME_DIRECTIVE
from argosy.agents.base import BaseAgent


# --------------------------------------------------------------------------
# Output schemas (flat — the bundled claude.exe chokes on nested $defs, so we
# take the prose-JSON path like DeploymentAuthorAgent).
# --------------------------------------------------------------------------
class InstrumentSwapDecision(BaseModel):
    chosen_symbol: str
    chosen_name: str = ""
    isin: str = ""
    domicile: str = ""
    nvda_weight_pct: float = 0.0
    us_weight_pct: float = 0.0
    rationale: str
    runner_up_symbol: str = ""
    runner_up_reason: str = ""


class DiversifierAdjudication(BaseModel):
    gold_wins: bool
    chosen_symbol: str
    chosen_name: str = ""
    sleeve_pct: float = Field(ge=0.0, le=10.0)
    gold_verdict_md: str
    rationale: str
    funding_note: str = ""


def _facts_block(candidates: list[dict[str, Any]]) -> str:
    lines = []
    for c in candidates:
        parts = [f"{c.get('symbol')}: {c.get('name', '')}"]
        for k in ("isin", "domicile", "ter", "aum", "index", "nvda_weight_pct",
                  "us_weight_pct", "yield_pct", "character", "notes"):
            if c.get(k) not in (None, ""):
                parts.append(f"{k}={c[k]}")
        lines.append("  - " + "; ".join(str(p) for p in parts))
    return "\n".join(lines) or "  (none)"


def _book_block(book: dict[str, Any]) -> str:
    holdings = book.get("holdings") or {}
    hl = "\n".join(
        f"    - {s}: ${v:,.0f}" for s, v in sorted(holdings.items(), key=lambda kv: -kv[1])
    )
    return (
        f"  - tradeable book: ${book.get('book_usd', 0):,.0f}\n"
        f"  - NVDA look-through TODAY: {book.get('nvda_lookthrough_pct', 0):.1f}% "
        f"(transition; plan glide sells it down toward the 12% direct target / 13% cap)\n"
        f"  - US-facing look-through TODAY: {book.get('us_facing_pct', 0):.1f}%\n"
        f"  - household income: NVIDIA salary (same complex as the equity concentration)\n"
        f"  - HOLDINGS (USD):\n{hl}"
    )


def _plan_block(plan_targets: dict[str, float]) -> str:
    return "\n".join(
        f"  - {label}: {pct}%" for label, pct in plan_targets.items()
    ) or "  (none)"


class SleeveInstrumentAuthorAgent(BaseAgent[InstrumentSwapDecision]):
    """Authors ONE sleeve-primary instrument pick from a sourced candidate table."""

    agent_role = "plan_instrument_author"
    output_model = InstrumentSwapDecision
    require_citations = False
    use_structured_output = False
    claude_code_max_retries = 1

    def build_prompt(
        self,
        *,
        sleeve_mandate: str,
        constraints: str,
        candidates: list[dict[str, Any]],
        book: dict[str, Any],
        plan_targets: dict[str, float],
    ) -> tuple[str, str]:
        system = (
            "You are the plan-instrument author on the Argosy fleet, choosing the "
            "PRIMARY instrument for one sleeve of a long-hold, Israeli-resident "
            "(non-US-person) investor's strategic plan.\n\n"
            f"{PRIME_DIRECTIVE}\n\n"
            "Choose from the SOURCED CANDIDATE FACTS only — never invent an "
            "instrument or trust a label over the sourced weights. Reason from the "
            "book's ACTUAL exposures (look-through, not fund names). Record an "
            "honest rationale: what the pick gives up as well as what it fixes.\n\n"
            "OUTPUT: a single JSON object with keys chosen_symbol, chosen_name, "
            "isin, domicile, nvda_weight_pct, us_weight_pct, rationale, "
            "runner_up_symbol, runner_up_reason. No prose outside the JSON."
        )
        user = (
            f"SLEEVE MANDATE:\n{sleeve_mandate}\n\n"
            f"HARD CONSTRAINTS:\n{constraints}\n\n"
            f"THE BOOK TODAY (raw):\n{_book_block(book)}\n\n"
            f"PLAN TARGET SLEEVES (current plan v64):\n{_plan_block(plan_targets)}\n\n"
            f"SOURCED CANDIDATE FACTS (cited from issuer/justETF factsheets):\n"
            f"{_facts_block(candidates)}\n\n"
            "Author the pick now."
        )
        return system, user


class SleeveInstrumentBlindReviewerAgent(BaseAgent[InstrumentSwapDecision]):
    """BLIND re-derivation of the same instrument choice — sees the same raw
    facts, NEVER the author's pick or reasoning. Code compares the two picks;
    divergence is surfaced, not auto-resolved."""

    agent_role = "plan_instrument_blind_reviewer"
    output_model = InstrumentSwapDecision
    require_citations = False
    use_structured_output = False
    claude_code_max_retries = 1

    def build_prompt(
        self,
        *,
        sleeve_mandate: str,
        constraints: str,
        candidates: list[dict[str, Any]],
        book: dict[str, Any],
        plan_targets: dict[str, float],
    ) -> tuple[str, str]:
        system = (
            "You are an independent reviewer on the Argosy fleet. ANOTHER agent has "
            "already chosen a primary instrument for the sleeve below — you have NOT "
            "seen its choice or reasoning, and you must not try to guess it. Your job "
            "is to RE-DERIVE the best pick yourself, from the raw sourced facts alone, "
            "as a check against the author. Be adversarial with every candidate: "
            "verify each one actually satisfies the hard constraints from its sourced "
            "numbers before considering it.\n\n"
            f"{PRIME_DIRECTIVE}\n\n"
            "OUTPUT: a single JSON object with keys chosen_symbol, chosen_name, isin, "
            "domicile, nvda_weight_pct, us_weight_pct, rationale, runner_up_symbol, "
            "runner_up_reason. No prose outside the JSON."
        )
        user = (
            f"SLEEVE MANDATE:\n{sleeve_mandate}\n\n"
            f"HARD CONSTRAINTS:\n{constraints}\n\n"
            f"THE BOOK TODAY (raw):\n{_book_block(book)}\n\n"
            f"PLAN TARGET SLEEVES (current plan v64):\n{_plan_block(plan_targets)}\n\n"
            f"SOURCED CANDIDATE FACTS (cited from issuer/justETF factsheets):\n"
            f"{_facts_block(candidates)}\n\n"
            "Derive your own pick now."
        )
        return system, user


_DIVERSIFIER_SYSTEM = (
    "You are adjudicating the DIVERSIFIER SLEEVE for a long-hold, Israeli-resident "
    "(non-US-person) investor whose book is heavily concentrated in the NVDA/US-tech/"
    "USD complex AND whose salary is NVIDIA. The plan currently holds 0% in anything "
    "uncorrelated with that complex.\n\n"
    "THE QUESTION ON TRIAL: should the new ~3-5% sleeve be a small PHYSICAL-GOLD "
    "slice, or a GROWTH-BEARING diversifier? The client rejected gold as a default "
    "('investing in a metal is lame') but added: 'I am not the expert — if gold is "
    "the right move I will add it.' So adjudicate ON THE MERITS, with the burden of "
    "proof ON GOLD: gold must beat the best growth-bearing alternative, not merely "
    "diversify.\n\n"
    f"{PRIME_DIRECTIVE}\n\n"
    "DECISION CRITERION: earliest SAFE retirement — the total-portfolio outcome on "
    "this household's ACTUAL book (concentration + NVIDIA employment income), NOT "
    "volatility-damping for its own sake. A diversifier that damps volatility but "
    "drags expected return can DELAY retirement; a 'diversifier' that re-buys the "
    "US/tech complex diversifies nothing. Weigh both failure modes.\n"
    "  - If GOLD wins, the rationale MUST explicitly answer the 'gold produces "
    "nothing' objection (why a non-yielding metal still buys an earlier safe "
    "retirement for THIS book).\n"
    "  - If gold LOSES, say so explicitly and pick the growth-bearing diversifier.\n"
    "  - Also note (funding_note) which over-tilted US sleeves should be trimmed "
    "to fund the sleeve, in ~whole percentage points.\n\n"
    "Constraints: instruments must be Irish/Lux UCITS or an Irish ETC (estate-gated "
    "core; non-US-situs for a non-US-person). One primary instrument for the sleeve. "
    "Sleeve size 3-5% — you decide the exact number.\n\n"
    "OUTPUT: a single JSON object with keys gold_wins (bool), chosen_symbol, "
    "chosen_name, sleeve_pct (number), gold_verdict_md, rationale, funding_note. "
    "No prose outside the JSON."
)


class DiversifierAdjudicatorAgent(BaseAgent[DiversifierAdjudication]):
    """Adjudicates gold-vs-growth-diversifier on the merits for the household book."""

    agent_role = "plan_diversifier_adjudicator"
    output_model = DiversifierAdjudication
    require_citations = False
    use_structured_output = False
    claude_code_max_retries = 1

    def build_prompt(
        self,
        *,
        candidates: list[dict[str, Any]],
        evidence_md: str,
        book: dict[str, Any],
        plan_targets: dict[str, float],
        blind_rederive: bool = False,
    ) -> tuple[str, str]:
        system = _DIVERSIFIER_SYSTEM
        if blind_rederive:
            system = (
                "You are an INDEPENDENT reviewer: another agent has already "
                "adjudicated this question — you have NOT seen its verdict and must "
                "not guess it; re-derive your own from the raw facts alone (your "
                "verdict is compared in code and divergence is surfaced).\n\n"
            ) + system
        user = (
            f"THE BOOK TODAY (raw):\n{_book_block(book)}\n\n"
            f"PLAN TARGET SLEEVES (current plan v64):\n{_plan_block(plan_targets)}\n\n"
            f"SOURCED CANDIDATE FACTS (cited):\n{_facts_block(candidates)}\n\n"
            f"SOURCED EVIDENCE (both sides of the gold debate):\n{evidence_md}\n\n"
            "Adjudicate now."
        )
        return system, user


__all__ = [
    "InstrumentSwapDecision",
    "DiversifierAdjudication",
    "SleeveInstrumentAuthorAgent",
    "SleeveInstrumentBlindReviewerAgent",
    "DiversifierAdjudicatorAgent",
]
