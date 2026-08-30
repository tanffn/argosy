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

from pydantic import BaseModel, Field, field_validator, model_validator

from argosy.agents.base import BaseAgent

Lens = Literal[
    "concentration", "diversification", "prudence", "candidate_selection", "sizing",
]

_LENS_BRIEF: dict[str, str] = {
    "concentration": (
        "single-name / factor concentration. The book is already dangerously "
        "concentrated in NVDA. Judge the PROPOSED TRADE'S effect on aggregate "
        "portfolio look-through, using both position size and constituent weight. "
        "Do not object merely because a diversified ETF contains NVDA, and do not "
        "apply an arbitrary per-fund NVDA cutoff: a 6.5%-NVDA world ETF materially "
        "diversifies capital away from 100%-NVDA stock. The mandate is to deploy the "
        "cash, so replacing 0%-NVDA cash with a plan-approved broad ETF is not, by "
        "itself, a concentration objection. Judge the funded plan including scheduled "
        "single-stock sales. BLOCK only when the chosen instrument and size make the "
        "approved whole-portfolio cap infeasible or add concentrated direct exposure. "
        "A no-action proposal does not worsen a legacy concentration and is not, by "
        "itself, a reason to force an unscheduled sale; the standing glide remains "
        "the funding plan. Object to an omitted sale only when a CURRENT autonomous "
        "SELL/TRIM signal requires it, and respect execution blockers on that signal. "
        "A role-equivalent, materially lighter alternative may merit a warning, never "
        "a veto merely because the selected broad ETF owns the concentrated name. "
        "A single-company stock has no constituent look-through to retrieve; judge "
        "its direct position size and thesis sleeve instead of calling it opaque."
    ),
    "diversification": (
        "whether each buy is TRUE diversification. OBJECT when a buy is labelled or "
        "implied to diversify but actually re-buys existing exposure (US-heavy "
        "'ex-US' funds, growth funds that are mega-cap/NVDA clones, a US-dividend "
        "fund redundant with one already held). A broad US/global ETF is still strong "
        "single-stock diversification from direct NVDA and must not be rejected merely "
        "because it owns NVDA. Reward genuine ex-US / uncorrelated adds without "
        "pretending every sleeve must have zero overlap. A single-company moonshot "
        "is intentionally direct exposure, not a diversified fund; missing holdings "
        "look-through is therefore not a defect."
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
    "candidate_selection": (
        "portfolio-level selection among the fresh discovery finalists, including "
        "the choice to buy NONE, ONE, or MULTIPLE names. Independently compare all "
        "research-BUY finalists using cap math, cash runway versus catalyst, "
        "dilution/leverage, catalyst quality, and independent multi-shot versus "
        "single-binary risk. A sub-1% position is not disqualified by size when it "
        "has a genuine evidence-backed >=5x convexity case; judge whether its upside "
        "could materially affect the whole portfolio. Prefer a split when two "
        "qualifying candidates have meaningfully independent failure modes; prefer "
        "concentration only when one clearly dominates. Plan gaps are context, not a "
        "requirement to touch every core sleeve, and 'position-size floor' alone is "
        "never a valid reason to omit the entire moonshot sleeve. ISSUER funding "
        "(cash runway / 'funded optionality') is evidence about company survival, "
        "NEVER investor deployable cash or an order-funding source. An omitted name "
        "may be added only by naming a feasible source from VERIFIED investor cash, "
        "verified after-tax sell proceeds, a reduction to another proposed buy, or a "
        "reduction to proposed reserve. Never recommend an unfunded order. Radar rank is search "
        "priority, not the final investment grade. BLOCK when the zero/one/multiple "
        "choice lacks an evidence-based comparative case or an omitted finalist "
        "clearly dominates. Any recommendation to add an omitted name uses impact "
        "adds_omitted_candidate even when severity is warn, and state the funding "
        "source in the concern; severity never decides whether the executable order "
        "must change. A research-BUY finalist is an opportunity, not a command to "
        "manufacture funding. When proposed verified funding is zero, do not originate "
        "a new sale solely to buy a finalist. You may object to how existing proposed "
        "cash, reserve, buys, or verified net sell proceeds are allocated, or surface "
        "an unfunded switch as advisory, but an action-changing objection must conserve "
        "the funding already present in the proposal."
    ),
    "sizing": (
        "independent sizing of every single-name convexity buy. Re-derive a rough, "
        "honestly uncertain terminal distribution from the raw program stage, market "
        "cap, runway/dilution, catalyst, failure-mode correlation, industry base rates, "
        "and the current book. A headline 10x possibility is not conviction. Judge "
        "whether the dollar amount is warranted by economic-wipeout probability, "
        "probability-weighted payoff, maximum portfolio loss, and upside contribution. "
        "Use one exact terminal date inside packet.horizon_years, annualize the rough "
        "distribution, apply the headline Israeli CGT to gains, and compare the result "
        "with cash/short bonds and diversified core opportunity cost. A positive "
        "terminal multiple is not enough when the edge is thin and confidence low. "
        "Small existing tracking positions are not sizing precedents. BLOCK when the "
        "amount requires materially stronger probabilities than the evidence supports. "
        "If a smaller starter would preserve useful convexity, use impact "
        "changes_amount and provide recommended_amount_usd. That is an action-changing "
        "disagreement even when severity is warn."
    ),
}


class ReviewObjection(BaseModel):
    ticker: str
    concern: str
    severity: Literal["block", "warn"]
    impact: Literal[
        "advisory_only",
        "changes_amount",
        "changes_ticker",
        "adds_omitted_candidate",
        "rejects_trade",
    ] | None = None
    recommended_amount_usd: float | None = Field(default=None, ge=0)
    recommended_ticker: str | None = None

    @field_validator("severity", mode="before")
    @classmethod
    def normalize_severity(cls, value: Any) -> str:
        """Normalize ordinary reviewer vocabulary to the two wire values."""
        normalized = str(value or "").strip().lower()
        if normalized in {"warning", "caution", "info", "advisory"}:
            return "warn"
        if normalized in {"blocking", "blocker", "critical", "reject"}:
            return "block"
        return normalized

    @model_validator(mode="after")
    def normalize_impact(self) -> ReviewObjection:
        # Backward-compatible fail-safe: a legacy block is always material;
        # a legacy warning remains advisory only when it recommends no change.
        if self.impact is None:
            self.impact = "rejects_trade" if self.severity == "block" else "advisory_only"
        if self.impact == "changes_amount" and self.recommended_amount_usd is None:
            raise ValueError("changes_amount requires recommended_amount_usd")
        if self.impact == "changes_ticker" and not (self.recommended_ticker or "").strip():
            raise ValueError("changes_ticker requires recommended_ticker")
        self.ticker = self.ticker.strip().upper()
        if self.recommended_ticker:
            self.recommended_ticker = self.recommended_ticker.strip().upper()
        return self


class DeploymentReviewOutput(BaseModel):
    lens: str
    objections: list[ReviewObjection] = Field(default_factory=list)
    overall_note: str = ""


class DeploymentReviewerAgent(BaseAgent[DeploymentReviewOutput]):
    """Blind judgment review of a deploy proposal's funded actions through one lens."""

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
            + (
                f" [source/role: {f.get('source')}; {f.get('confidence')}]"
                if f.get("source") else ""
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
        finalist_lines = "\n".join(
            "  - {ticker}: radar rank {rank}, score {score}, fresh {fresh}; "
            "research {verdict}/{conviction}; thesis={thesis}".format(
                ticker=row.get("ticker"),
                rank=row.get("rank"),
                score=row.get("score"),
                fresh=row.get("fresh_as_of"),
                verdict=(row.get("fleet") or {}).get("verdict"),
                conviction=(row.get("fleet") or {}).get("conviction"),
                thesis=str((row.get("fleet") or {}).get("thesis_md") or "")[:2200],
            )
            for row in (packet.get("discovery_candidates") or [])
            if str((row.get("fleet") or {}).get("verdict") or "").upper() == "BUY"
        ) or "  (none)"
        recommendation_lines = "\n".join(
            "  - {action} {ticker}; verification={verification}; "
            "confidence={confidence}; evidence={rationale}".format(
                action=str(row.get("action") or "").upper(),
                ticker=str(row.get("ticker") or "").upper(),
                verification=row.get("verification_status") or "confirmed",
                confidence=row.get("confidence") or "unknown",
                rationale=str(row.get("rationale") or "")[:900],
            )
            for row in (packet.get("current_recommendations") or [])
        ) or "  (none)"
        funding = packet.get("review_funding") or {}
        tax_lot_lines = "\n".join(
            f"  - {ticker}: {facts.get('meaning', 'unknown')}"
            for ticker, facts in sorted(
                (packet.get("tax_lot_coverage") or {}).items()
            )
        ) or "  (none recorded)"
        sell_lines = "\n".join(
            f"  - SELL/TRIM {row.get('symbol')} "
            f"${float(row.get('amount_usd') or 0):,.0f} gross"
            for row in (packet.get("proposed_sells") or [])
        ) or "  (none)"
        disposition_lines = "\n".join(
            "  - {ticker}: {selection}; authored size ${amount:,.0f}; reason withheld".format(
                ticker=row.get("ticker"),
                selection=row.get("selection") or "UNKNOWN",
                amount=float(row.get("recommended_position_usd") or 0),
            )
            for row in (packet.get("author_candidate_dispositions") or [])
        ) or "  (none recorded)"
        # BLIND: buys carry ticker/amount/sleeve only — NOT the author's rationale.
        buy_lines = "\n".join(
            f"  - BUY {b.get('symbol')} ${b.get('amount_usd', 0):,.0f} (sleeve: {b.get('sleeve','')})"
            for b in buys
        ) or "  (none)"

        system = (
            "You are a reviewer on Argosy's deployment team, for a long-hold, "
            "Israeli-resident (non-US-person) investor. Another agent PROPOSED the "
            "funded actions below; you have NOT seen its reasoning. Re-derive independently "
            "from the RAW data and OBJECT, by your own judgment, to anything unsound.\n\n"
            f"YOUR LENS is {lens}: {_LENS_BRIEF.get(lens, lens)}\n\n"
            "Judge from the RAW facts, not from an action's label or any assumed "
            "rationale. Compare before/after portfolio exposure; constituent presence "
            "alone is never a reason to object. For any single-name concentration "
            "concern, do the direction-of-change arithmetic: when cash funds an ETF "
            "whose weight in that name is below the book's current look-through "
            "percentage, the trade DILUTES that concentration. Do not call such a "
            "trade re-buying or re-concentration. An objection then requires a "
            "separate, concrete defect and must quantify the before/after harm or a "
            "materially better role-equivalent alternative. "
            "The accepted PLAN MENU is authoritative for each ticker's intended sleeve "
            "role. You may challenge whether an implementation serves that role, but "
            "do not relabel a quality, income, low-volatility, or other factor fund from "
            "geography alone; name contradictory sourced strategy facts if you do. "
            "Raise a `block` objection for a "
            "genuinely unsound buy, `warn` "
            "for a concern worth surfacing. Severity expresses urgency only. Set "
            "`impact=advisory_only` only when the executable ticker and amount may "
            "remain unchanged. Use `changes_amount` with `recommended_amount_usd`, "
            "`changes_ticker` with `recommended_ticker`, `adds_omitted_candidate`, or "
            "`rejects_trade` whenever following your judgment changes the order list. "
            "The arithmetic verifier already checked the stated funding totals. Do "
            "not create additional investor cash. Company cash runway is not order "
            "funding. Tax-lot availability is not verified net sale proceeds and does "
            "not authorize you to originate a sale. The verified funding figures below "
            "are the complete funding envelope for this review. When recommending an omitted buy, name which verified funding "
            "source changes and keep the revised order set conserved. Also review "
            "proposed SELL/TRIM actions and disputed holding signals under your lens; "
            "do not silently convert an actionable signal into HOLD. Missing cost "
            "basis is a real execution blocker under the required after-tax contract: "
            "surface the blocked signal, but do not demand a fabricated executable "
            "sale or spend its unknown proceeds. If an action is "
            "sound under your lens, say "
            "nothing about it. Be specific and concise: name the ticker and give the "
            "concrete reason in no more than two sentences."
        )
        user = (
            f"CURRENT CONCENTRATION: {nvda.get('pct', 0)}% NVDA look-through "
            f"(${nvda.get('lookthrough_usd', 0):,.0f} of ${nvda.get('book_usd', 0):,.0f}), "
            f"cap {nvda.get('cap_pct', 0)}%.\n\n"
            f"REQUESTED TERMINAL HORIZON (years): "
            f"{packet.get('horizon_years') or [1, 5]}.\n\n"
            "VERIFIED INVESTOR FUNDING (issuer cash/runway is unrelated):\n"
            f"  - new deployable cash: ${float(funding.get('deployable_usd') or 0):,.0f}\n"
            f"  - verified net sell proceeds: "
            f"${float(funding.get('verified_net_sell_proceeds_usd') or 0):,.0f}\n"
            f"  - proposed buys: ${float(funding.get('proposed_buys_usd') or 0):,.0f}\n"
            f"  - proposed reserve: ${float(funding.get('proposed_reserve_usd') or 0):,.0f}\n\n"
            f"RAW INSTRUMENT LOOK-THROUGH FACTS:\n{facts_lines}\n\n"
            f"PLAN SLEEVE TARGETS:\n{menu_lines}\n\n"
            f"CURRENT HOLDINGS:\n{holdings_lines}\n\n"
            f"TAX-LOT / COST-BASIS COVERAGE:\n{tax_lot_lines}\n\n"
            f"CURRENT AUTONOMOUS BUY/SELL/TRIM SIGNALS:\n{recommendation_lines}\n\n"
            f"FRESH RESEARCH-BUY DISCOVERY FINALISTS:\n{finalist_lines}\n\n"
            "AUTHOR CANDIDATE DISPOSITIONS (selection receipt only; reason withheld):\n"
            f"{disposition_lines}\n\n"
            f"PROPOSED BUYS (ticker / amount / sleeve only — reason withheld):\n{buy_lines}\n\n"
            f"PROPOSED SELLS/TRIMS (ticker / gross amount only; reason withheld):\n"
            f"{sell_lines}\n\n"
            'Return a JSON object {"lens": str, "objections": [{"ticker": str, '
            '"concern": str, "severity": "block|warn", "impact": '
            '"advisory_only|changes_amount|changes_ticker|adds_omitted_candidate|rejects_trade", '
            '"recommended_amount_usd": number|null, "recommended_ticker": str|null}], '
            '"overall_note": str}.'
        )
        return system, user


__all__ = ["DeploymentReviewerAgent", "DeploymentReviewOutput", "ReviewObjection", "Lens"]
