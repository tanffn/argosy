"""DeploymentReviewerAgent — a BLIND judgment reviewer on the deploy team.

The team, not a gate: the author proposes an allocation; independent reviewers
re-derive from the RAW data (holdings, look-through facts, plan targets) — WITHOUT
seeing the author's rationale — and object by judgment to anything unsound. This is
how the R1GR-class miss is caught: a concentration reviewer independently sees that
R1GR is ~14% NVDA and refutes "diversifier", no per-symptom gate required.

One reviewer per LENS (concentration / diversification-truth / prudence), so the
team covers distinct failure modes instead of N correlated copies
([[feedback_adversarial_review_must_re_derive_blind]]). Determinism stays out of
judgment — it only guards inviolable arithmetic elsewhere.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from argosy.agents.base import BaseAgent

Lens = Literal["concentration", "diversification", "prudence"]

_LENS_BRIEF: dict[str, str] = {
    "concentration": (
        "single-name / factor concentration. The book is already dangerously "
        "concentrated in NVDA. OBJECT to any buy that ADDS meaningful NVDA (or other "
        "single-name) look-through — including funds that are themselves NVDA-heavy "
        "(a growth index whose top holding is NVDA is NOT a diversifier). Use the "
        "look-through facts; a broad fund's incidental few-percent NVDA is fine, a "
        "double-digit NVDA weight is not."
    ),
    "diversification": (
        "whether each buy is TRUE diversification. OBJECT when a buy is labelled or "
        "implied to diversify but actually re-buys existing exposure (US-heavy "
        "'ex-US' funds, growth funds that are mega-cap/NVDA clones, a US-dividend "
        "fund redundant with one already held). Reward genuine ex-US / uncorrelated adds."
    ),
    "prudence": (
        "overall prudence for a long-hold, non-US-person investor given the CURRENT "
        "book. OBJECT to imprudent moves: piling into an already-extended/over-target "
        "sleeve, new US-situs estate exposure beyond the sanctioned sleeve, or "
        "additive buys that duplicate a held position instead of migrating it. "
        "Derive US-situs from the provided incorporation/domicile facts, never from "
        "the buy's claimed weights — a US-listed, US-incorporated company is US-situs "
        "even when its economics/revenue are foreign."
    ),
}


class ReviewObjection(BaseModel):
    ticker: str
    concern: str
    severity: Literal["block", "warn"]


class DeploymentReviewOutput(BaseModel):
    lens: str
    objections: list[ReviewObjection] = Field(default_factory=list)
    overall_note: str = ""


class DeploymentReviewerAgent(BaseAgent[DeploymentReviewOutput]):
    """Blind judgment review of a deploy proposal's buys through one lens."""

    agent_role = "deployment_reviewer"
    output_model = DeploymentReviewOutput
    require_citations = False

    def build_prompt(self, *, lens: str, packet: dict[str, Any], buys: list[dict[str, Any]]):
        nvda = packet.get("nvda") or {}
        facts = packet.get("instrument_facts") or []
        # RAW look-through per instrument — the ground truth the reviewer re-derives
        # from. (US weight is sourced; NVDA look-through is added by the team layer.)
        facts_lines = "\n".join(
            f"  - {f.get('symbol')}: {f.get('us_weight', 0) * 100:.0f}% US"
            + (f", ~{f.get('nvda_weight', 0) * 100:.0f}% NVDA" if f.get("nvda_weight") is not None else "")
            + (
                f", {'US-SITUS (estate-exposed)' if f['us_situs'] else 'non-US-situs'}"
                f" [domicile: {f.get('domicile', 'n/a')}]"
                if f.get("us_situs") is not None else ""
            )
            for f in facts
        ) or "  (none)"
        menu_lines = "\n".join(
            f"  - {m.get('sleeve')}: target {m.get('target_pct')}%"
            + (f", current {m.get('current_pct')}%" if "current_pct" in m else "")
            for m in (packet.get("plan_menu") or [])
        ) or "  (none)"
        holdings_lines = "\n".join(
            f"  - {s}: ${v:,.0f}" for s, v in sorted(
                (packet.get("holdings") or {}).items(), key=lambda kv: -kv[1])
        ) or "  (none)"
        # BLIND: buys carry ticker/amount/sleeve only — NOT the author's rationale.
        buy_lines = "\n".join(
            f"  - BUY {b.get('symbol')} ${b.get('amount_usd', 0):,.0f} (sleeve: {b.get('sleeve','')})"
            for b in buys
        ) or "  (none)"

        system = (
            "You are a reviewer on Argosy's deployment team, for a long-hold, "
            "Israeli-resident (non-US-person) investor. Another agent PROPOSED the "
            "buys below; you have NOT seen its reasoning. Re-derive independently "
            "from the RAW data and OBJECT, by your own judgment, to anything unsound.\n\n"
            f"YOUR LENS is {lens}: {_LENS_BRIEF.get(lens, lens)}\n\n"
            "Judge from the RAW facts, not from the buy's label or any assumed "
            "rationale. Raise a `block` objection for a genuinely unsound buy, `warn` "
            "for a concern worth surfacing. If a buy is sound under your lens, say "
            "nothing about it. Be specific: name the ticker and the concrete reason."
        )
        user = (
            f"CURRENT CONCENTRATION: {nvda.get('pct', 0)}% NVDA look-through "
            f"(${nvda.get('lookthrough_usd', 0):,.0f} of ${nvda.get('book_usd', 0):,.0f}), "
            f"cap {nvda.get('cap_pct', 0)}%.\n\n"
            f"RAW INSTRUMENT LOOK-THROUGH FACTS:\n{facts_lines}\n\n"
            f"PLAN SLEEVE TARGETS:\n{menu_lines}\n\n"
            f"CURRENT HOLDINGS:\n{holdings_lines}\n\n"
            f"PROPOSED BUYS (ticker / amount / sleeve only — reason withheld):\n{buy_lines}\n\n"
            'Return a JSON object {"lens": str, "objections": [{"ticker": str, '
            '"concern": str, "severity": "block|warn"}], "overall_note": str}.'
        )
        return system, user


__all__ = ["DeploymentReviewerAgent", "DeploymentReviewOutput", "ReviewObjection", "Lens"]
