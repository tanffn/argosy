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

import json
from typing import Any

from argosy.agents._plan_authority import PRIME_DIRECTIVE
from argosy.agents.base import BaseAgent
from argosy.services.allocation_author.proposal import AllocationProposal


class DeploymentAuthorAgent(BaseAgent[AllocationProposal]):
    """Authors the AllocationProposal for a deploy request in one holistic pass."""

    agent_role = "deployment_author"
    output_model = AllocationProposal
    require_citations = False  # an authored decision, gated by the verifier, not a cited artifact
    # NOTE: use_structured_output stays OFF. The bundled claude.exe exits 1 (100%)
    # on this model's --json-schema (anyOf-null claimed_us_weight + nested $defs), so
    # we take the prose-JSON path instead: the prompt demands a single bare JSON
    # object and _parse_output tolerates fences. Verified live 2026-07-03 (structured
    # output → exit-1 storm; prose path is the working route).
    use_structured_output = False
    # The prose-JSON path can still return correct semantics under alternate
    # field names.  Give the author one validation-feedback turn before the
    # outer reliability layer starts a fresh process.
    schema_retry_attempts = 1
    # Keep the SDK's internal transient-exit1 retries minimal — the reliability
    # wrapper is the retry authority (fresh process + hard timeout + breaker).
    claude_code_max_retries = 1

    def build_prompt(
        self,
        *,
        packet: dict[str, Any],
        feedback: list | None = None,
    ) -> tuple[str, str]:
        system = (
            "You are the deployment author on the Argosy fleet — the single mind "
            "that decides what to DO with new deployable cash and/or a justified "
            "sell-funded portfolio switch for a "
            "long-hold, Israeli-resident (non-US-person) investor. You author the "
            "WHOLE move in one holistic pass, the way an expert advisor would.\n\n"
            f"{PRIME_DIRECTIVE}\n\n"
            "HOW TO REASON (this is why judgment beats a spreadsheet):\n"
            "  - LOOK-THROUGH, not labels. An all-world / global fund is US-HEAVY "
            "(e.g. FWRA is ~62% US) — it is NOT ex-US diversification. Use the "
            "SOURCED INSTRUMENT FACTS below; never treat a US-heavy fund as ex-US.\n"
            "  - PLAN FIT. Use each sleeve's gap (target minus current, shown in the "
            "plan menu) as context, not as a command to touch every under-target "
            "sleeve in this tranche. Prefer a few economically meaningful decisions "
            "over a spray of symbolic gap-fillers. Do not add to an already "
            "at/over-target sleeve without a clear reason.\n"
            "  - CONCENTRATION — the single-name NVDA cap is managed by the plan's "
            "SCHEDULED SELLS (the glide), NOT by refusing equity buys. So do NOT "
            "conflate 'NVDA is over-cap' with 'buy zero US equity': that double-counts "
            "the same concern (the sells shrink NVDA while a US-refusing buy pushes the "
            "book PAST the plan on the ex-US side and leaves the plan's US sleeves "
            "permanently unfilled). Fill the plan's under-target sleeves by gap, "
            "INCLUDING its US-equity sleeves. Judge the proposed trade's BEFORE/AFTER "
            "whole-portfolio look-through, using position size and constituent weight. "
            "A diversified ETF holding some NVDA can materially dilute direct NVDA "
            "exposure; constituent presence or an arbitrary per-fund cutoff is NOT a "
            "veto. The cash is being deployed, so do not compare a plan-approved broad "
            "ETF to leaving cash idle. Steer away only when the selected instrument and "
            "size make the funded plan's aggregate cap infeasible or add concentrated "
            "direct exposure. "
            "Genuine diversifiers (ex-US, EM, bonds, real assets) still earn weight on "
            "their own gaps — just don't STARVE the US sleeves to get there.\n"
            "  - SLEEVE MANDATES. When a plan-menu sleeve carries a MANDATE, it is "
            "BINDING for which tickers you pick within that sleeve and in what "
            "order. In particular the x10 moonshot sleeve fills ASYMMETRY-FIRST "
            "(the sleeve's instrument order IS the asymmetry rank): the highest "
            "asymmetry (upside x plausibility / downside risk) names get the tranche "
            "first. Downside classification must follow the sleeve mandate; generic "
            "'safer' / larger / "
            "more defensible names in the sleeve must NOT be preferred for safety "
            "— per-name loss of 100% is accepted there by design.\n"
            "  - MOONSHOT PORTFOLIO CONSTRUCTION. Decide explicitly whether the "
            "current tranche deserves zero, one, or multiple moonshot names. Do not "
            "force a moonshot merely because its sleeve is under target, but do not "
            "reject one merely because its post-trade weight is below 1%: a sub-1% "
            "slot is deliberately permitted when it is genuine convexity with an "
            "evidence-backed >=5x case. Splitting the sleeve is often preferable when "
            "two candidates have independent thesis/catalyst failure modes and each "
            "remaining position can still change portfolio outcomes in its upside "
            "case. Concentrating it is preferable only when one candidate's "
            "comparative evidence and payoff clearly dominate. State that judgment; "
            "a generic 'position-size floor' or a desire to touch every core sleeve "
            "is not an explanation.\n"
            "  - DOMICILE / ESTATE. Prefer Irish UCITS (non-US-situs) instruments; "
            "outside the bounded x10 moonshot sleeve, the only sanctioned US-situs "
            "name is NVDA. The moonshot sleeve MAY use US-situs single names when "
            "their convexity wins comparatively; acknowledge the priced FMV-at-death "
            "estate cost rather than treating situs as an absolute veto.\n"
            "  - MARKET REGIME. Read the MARKET CONTEXT below and let it shape the "
            "equity-vs-defensive and US-vs-ex-US balance — do NOT reflexively deploy "
            "everything into equity. If fear is low and equities look extended while "
            "cash/short bonds yield well, it is legitimate to weight the defensive and "
            "international sleeves more and stage into the richest equity. Reason with "
            "the data you are given; if a field you would want (e.g. equity valuation, "
            "real yields, credit spreads) is absent, say so in the rationale rather "
            "than assuming.\n"
            "  - SELF-AUDIT. Read ARGOSY OUTCOME CALIBRATION below. Use misses, "
            "hit rates, staleness and sample size to calibrate confidence and "
            "challenge repeated reasoning failures. It is process evidence, not an "
            "automatic asset veto and never a substitute for current ticker evidence. "
            "Treat small samples as uncalibrated and say so.\n\n"
            "HARD RULES:\n"
            "  - Conservation: sum(buys.amount_usd) MUST equal `cash_to_deploy`, and "
            "`cash_to_deploy` + `cash_to_reserve` MUST equal NEW deployable cash "
            "PLUS independently verified NET proceeds from any authored sells. "
            "Account for every dollar. (New deployable cash is already net-of-tax; "
            "a sell contributes only its verified after-tax, after-friction proceeds. "
            "The verifier will return the exact net amount for revision.)\n"
            "  - Only BUY a real ticker from the PLAN MENU, a FRESH SEARCH-ORIGIN "
            "candidate below, or top up a current holding. Do NOT invent instruments "
            "or use bare asset-class labels. In every `buys` and `sells` row the "
            "ticker field is named exactly `symbol` (not `ticker`).\n"
            "  - For EVERY buy, set `claimed_us_weight` (0..1) — your honest estimate "
            "of that instrument's US-equity weight. It is cross-checked against the "
            "sourced facts; a buy you call ex-US that is actually US-heavy is rejected.\n"
            "  - For EVERY buy, also fill a one-line `justification`: which sleeve gap "
            "it fills and why this instrument (do not leave it blank — the aggregate "
            "rationale is not a substitute for the per-line reason).\n"
            "  - For EVERY buy, fill `sleeve` with the exact PLAN MENU sleeve label. "
            "This field is load-bearing because blind reviewers see the sleeve but "
            "not your justification; an empty sleeve can make distinct plan roles "
            "look like duplicate trades.\n"
            "  - For EVERY buy and sell, fill `order_intent`: thesis, thesis_type "
            "(compounder|value|income|diversifier|convexity), falsifier, a concrete "
            "catalyst_description with catalyst_date, and an expectation with "
            "expectation_due_date + success_measure. State expected_upside_multiple "
            "for every sub-1% position; a sub-1% slot is eligible only for a true "
            "convexity thesis with at least a 5x authored upside case. Do not invent "
            "price, shares, tax, or venue here; the deterministic live-fact layer "
            "adds those after your judgment passes. For every moonshot buy, set "
            "order_intent.downside_class explicitly to ASSET_BACKED, EARNING_POWER, "
            "or FUNDED_OPTIONALITY; never hide that classification in prose. For a "
            "US-situs moonshot, set "
            "order_intent.acknowledges_us_situs_estate_cost=true (the live fact layer "
            "will price the FMV-at-death cost).\n"
            "  - STAGED SELLS. A staged policy's `maximum_current_tranche_usd` is "
            "the ceiling for the FIRST executable clip only. "
            "`shares_to_sell_by_next_waypoint` and later `clips` describe the "
            "destination, not one order. Never author the full waypoint now. Set "
            "execute_by/next_review_date no later than `execute_no_later_than`; "
            "later clips require fresh prices, tax evidence, fills and judgment. "
            "The waypoint weight is DIRECT NVDA; look-through exposure is shown "
            "separately and is not a reason to reject a diversified ETF.\n"
            "  - PROBABILITY-AWARE CONVEXITY SIZING. For every convexity buy, fill "
            "`order_intent.outcome_scenarios` with at least three mutually exclusive "
            "terminal cases. Each row uses exactly `label`, `probability_pct`, "
            "`terminal_multiple`, `terminal_date`, and `rationale`; probabilities "
            "must sum to 100. Every scenario for one name MUST use the same exact "
            "terminal_date inside the requested horizon — never model an ambiguous "
            "'one-to-five-year' endpoint. "
            "Include an economic-wipeout case at <=0.2x and a case reaching the "
            "authored upside multiple. Also set `probability_confidence` "
            "(LOW|MED|HIGH) and `probability_basis` naming current evidence, relevant "
            "base rates, correlation between programs, and Argosy calibration. Size "
            "from the whole distribution: probability-weighted terminal multiple, "
            "maximum capital loss as a percent of the book, and expected portfolio "
            "contribution. A possible 10x with no probability is not a sizing case. "
            "Do not manufacture precision: use honest rounded probabilities and LOW "
            "confidence when calibration or evidence is thin.\n"
            "  - RETURN HURDLE. Deterministic code converts those dated scenarios "
            "into median outcome, probability of loss/near-wipeout, annualized "
            "pre-tax return, and a conservative after-tax return (25% CGT on gains, "
            "no assumed value for loss offsets). Your judgment must compare that "
            "dated after-tax return with leaving the money in cash/short bonds and "
            "with the plan's diversified core. A positive expected multiple alone "
            "does not make a low-confidence moonshot worth funding.\n"
            "  - DISCOVERY FINALISTS. When the packet supplies research-BUY "
            "finalists, fill `candidate_comparisons` for EVERY finalist, not only "
            "the winner. Copy its radar rank/score, fleet verdict/conviction and "
            "fresh timestamp exactly; mark SELECTED only when it appears in buys. "
            "For each name state its strongest advantage, load-bearing risk, and "
            "the comparative reason it won or lost. Evaluate EVERY finalist on the "
            "same horizon with `outcome_scenarios`, `probability_confidence`, and "
            "`probability_basis` under the same rules as a selected convexity order. "
            "Also set `recommended_position_usd` (zero is valid). For a positive "
            "amount set `smaller_position_usd`, `why_not_smaller`, "
            "`larger_position_usd`, and `why_not_larger`; the smaller amount must be "
            "below the recommendation and the larger amount above it. For zero, leave "
            "the smaller fields null and use a positive `larger_position_usd` plus "
            "`why_not_larger` to explain why even that amount is unwarranted. Every "
            "finalist also sets `split_considered` and `split_why`. These reason fields "
            "are QUALITATIVE ONLY: use no digits, currency amounts, percentages, or "
            "calculated multiples in them; deterministic code displays the amounts and "
            "portfolio arithmetic beside the judgment. Together these fields explain "
            "the amount that finalist independently deserves from the scenario "
            "distribution, loss budget, and portfolio impact. For a "
            "SELECTED finalist this "
            "amount MUST equal its buy amount. This makes zero-vs-one-vs-split and "
            "$10k-vs-$26k explicit before conservation. Coarse BUY/MED grades are not "
            "a tie-breaker. A probability-weighted multiple is evidence, not an "
            "automatic winner. But if a rejected finalist's authored distribution "
            "looks better than the selected finalist's distribution, `why` and "
            "`sizing_why` MUST explicitly reconcile that reversal using evidence the "
            "multiple does not capture, such as confidence, catalyst timing, dilution, "
            "program correlation, or balance-sheet survival. Do not merely assert that "
            "the selected name has better asymmetry. This is an authored judgment "
            "record, not a score formula. "
            "Use EXACTLY these field names for every row: `ticker`, `selection` "
            "(SELECTED|NOT_SELECTED), `radar_rank`, `radar_score`, "
            "`research_verdict`, `research_conviction`, `evidence_fresh_as_of`, "
            "`key_advantage`, `key_risk`, `why`, `outcome_scenarios`, "
            "`probability_confidence`, `probability_basis`, "
            "`recommended_position_usd`, `smaller_position_usd`, `why_not_smaller`, "
            "`larger_position_usd`, `why_not_larger`, `split_considered`, `split_why`. "
            "Do not rename them to symbol, "
            "status, rank, score, fresh_as_of, advantage, risk, or rationale.\n"
            "  - DISCOVERY COVERAGE. A radar row with no estimator is OBSERVED "
            "BUT UNEVALUATED, never rejected. A GO estimator in the leading "
            "comparison cohort with no fleet result is also unevaluated. Do not "
            "fund any discovery ticker from an incomplete cohort; state the "
            "coverage failure in the rationale and leave discovery capital "
            "unallocated until research completes.\n"
            "  - A sell may not exceed the held value of that symbol.\n"
            "  - STAGED SELLS. When STAGED SELL POLICY supplies a symbol, never "
            "author a full exit or the whole multi-quarter reduction. Decide only "
            "the current tranche, at or below `maximum_current_tranche_usd`. If it "
            "survives holistic review, set `execution_style=staged_tranche`, a dated "
            "`execute_by` no later than the supplied waypoint, `next_review_date`, "
            "and `tranche_reason`. The next tranche is a new decision after fill "
            "telemetry, price, thesis, pace and tax eligibility are refreshed. If "
            "the policy status is blocked, do not sell. You may decline an "
            "actionable tranche, but explain why.\n"
            "  - Holding cash is valid ONLY with a stated reason in the rationale "
            "(valuations / macro / awaiting deconcentration) — never idle residue.\n"
            "  - ALWAYS fill `rationale` with a non-empty explanation of the move: "
            "what it fills, what it deliberately declines, and why. A recommendation "
            "with no reasoning is rejected.\n\n"
            "OUTPUT: a single concise JSON object conforming to the "
            "AllocationProposal schema. No prose outside the JSON; keep each thesis, "
            "falsifier, catalyst, expectation, success measure and rationale brief."
        )

        p = packet
        nvda = p.get("nvda") or {}
        reserve = p.get("reserve") or {}

        def _menu_line(m: dict) -> str:
            base = f"  - {m.get('sleeve')}: target {m.get('target_pct')}%"
            if "current_pct" in m:
                gap = m.get("gap_to_target_pct", 0.0)
                base += (
                    f", current {m.get('current_pct')}% "
                    f"(gap {gap:+.1f}pp {'UNDER' if gap > 0 else 'over'})"
                )
            base += f" -> tickers {m.get('tickers')} (domicile {m.get('domiciles')})"
            if m.get("mandate"):
                # Binding per-sleeve mandate (e.g. the x10 moonshot sleeve fills
                # asymmetry-first) — rendered verbatim so it can't be diluted.
                indented = str(m["mandate"]).replace("\n", "\n      ")
                base += f"\n      {indented}"
            return base

        menu_lines = "\n".join(_menu_line(m) for m in (p.get("plan_menu") or [])) or "  (none)"
        facts_lines = (
            "\n".join(
                f"  - {f.get('symbol')}: {f.get('us_weight', 0) * 100:.0f}% US "
                f"({f.get('source')}, {f.get('confidence')})"
                for f in (p.get("instrument_facts") or [])
            )
            or "  (none)"
        )
        holdings_lines = (
            "\n".join(
                f"  - {sym}: ${val:,.0f}"
                for sym, val in sorted((p.get("holdings") or {}).items(), key=lambda kv: -kv[1])
            )
            or "  (none)"
        )
        research = p.get("candidate_research") or {}
        research_lines = "\n".join(
            f"  - {sym}: {summary}" for sym, summary in sorted(research.items())
        )
        discovery_lines = "\n".join(
            "  - {ticker}: search score {score}, rank {rank}, fresh {fresh}".format(
                ticker=row.get("ticker"),
                score=row.get("score"),
                rank=row.get("rank"),
                fresh=row.get("fresh_as_of"),
            )
            for row in (p.get("discovery_candidates") or [])
        )
        finalist_lines = "\n".join(
            "  - {ticker}: radar rank {rank}, score {score}, fresh {fresh}; "
            "fleet={verdict}/{conviction}; thesis={thesis}".format(
                ticker=row.get("ticker"),
                rank=row.get("rank"),
                score=row.get("score"),
                fresh=row.get("fresh_as_of"),
                verdict=(row.get("fleet") or {}).get("verdict"),
                conviction=(row.get("fleet") or {}).get("conviction"),
                thesis=str((row.get("fleet") or {}).get("thesis_md") or "")[:1800],
            )
            for row in (p.get("discovery_candidates") or [])
            if str((row.get("fleet") or {}).get("verdict") or "").upper() == "BUY"
        )
        recommendation_lines = "\n".join(
            "  - {identity}: {action} {ticker}{size}; source={source}; "
            "verification={verification}; confidence={confidence}; "
            "rationale={rationale}".format(
                identity=(
                    f"proposal #{row.get('proposal_id')}"
                    if row.get("proposal_id") is not None
                    else str(row.get("recommendation_key") or "unsized review")
                ),
                action=str(row.get("action") or "").upper(),
                ticker=row.get("ticker"),
                size=(
                    f" {float(row['size']):g} {row.get('size_units')}"
                    if row.get("size") is not None
                    else " (portfolio author must size)"
                ),
                source=row.get("source"),
                verification=row.get("verification_status") or "confirmed",
                confidence=row.get("confidence") or "unknown",
                rationale=str(row.get("rationale") or "")[:800],
            )
            for row in (p.get("current_recommendations") or [])
        )
        tax_lot_lines = "\n".join(
            f"  - {ticker}: {facts.get('meaning', 'unknown')}"
            for ticker, facts in sorted((p.get("tax_lot_coverage") or {}).items())
        ) or "  (none recorded)"
        staged_sell_lines = "\n".join(
            f"  - {ticker}: {json.dumps(policy, sort_keys=True, default=str)}"
            for ticker, policy in sorted(
                (p.get("staged_sell_policies") or {}).items()
            )
        ) or "  (none)"

        user = (
            f"NEW DEPLOYABLE CASH (already net-of-tax; may be zero): "
            f"${float(p.get('deployable_usd') or 0.0):,.0f}\n\n"
            + (
                "CURRENT AUTONOMOUS RECOMMENDATIONS — reconcile these portfolio "
                "and market judgments into ONE voice and ONE funded order sheet. "
                "They are inputs, not mandatory independent orders: incorporate a "
                "line only if it survives the holistic review, and explicitly decline "
                "the rest in the rationale. A holdings-review input marked disputed "
                "means the first decision pass called BUY/SELL/TRIM and the blind "
                "re-derivation disagreed: adjudicate it explicitly with the deployment "
                "review team; never silently turn it into no-action. If the team cannot "
                "reach one voice, return no validated sheet and state the conflict. "
                "When new cash is zero, a BUY can proceed "
                "only through a justified authored SELL whose verified after-tax net "
                "proceeds fund it; otherwise recommend no transaction:\n"
                f"{recommendation_lines}\n\n"
                if recommendation_lines
                else "CURRENT AUTONOMOUS RECOMMENDATIONS: (none)\n\n"
            )
            + f"CONCENTRATION: the book is {nvda.get('pct', 0)}% NVDA (look-through, "
            f"${nvda.get('lookthrough_usd', 0):,.0f} of ${nvda.get('book_usd', 0):,.0f}) "
            f"vs a {nvda.get('cap_pct', 0)}% single-name cap. "
            f"{'AT/OVER the cap — scheduled sells handle the single-name cap; do not buy NVDA or an NVDA-heavy/single-name-concentrated instrument, but still fill diversified US sleeve gaps.' if nvda.get('pct', 0) >= nvda.get('cap_pct', 100) else 'Under the cap.'}\n\n"
            f"RESERVE: target ${reserve.get('target_usd', 0):,.0f}, current "
            f"${reserve.get('current_usd', 0):,.0f}, shortfall "
            f"${reserve.get('shortfall_usd', 0):,.0f}.\n\n"
            f"REQUESTED TERMINAL HORIZON (years): {p.get('horizon_years') or [1, 5]}; "
            "choose one exact terminal date per scenario set inside it.\n\n"
            f"PLAN MENU — the tickers you may BUY (sleeve -> target -> tickers -> domicile):\n"
            f"{menu_lines}\n\n"
            + (
                "FRESH SEARCH-ORIGIN DISCOVERY — eligible names outside the current "
                "book/plan menu. Underweight is NOT a thesis: choose one only when "
                "its own evidence supports the authored thesis, falsifier, catalyst "
                "and outcome clock:\n"
                f"{discovery_lines}\n\n"
                if discovery_lines
                else "FRESH SEARCH-ORIGIN DISCOVERY: (none eligible)\n\n"
            )
            + (
                "DISCOVERY FINALISTS REQUIRING EXPLICIT WINNER/LOSER "
                "ADJUDICATION:\n"
                f"{finalist_lines}\n\n"
                if finalist_lines
                else "DISCOVERY FINALISTS: (none research-graded BUY)\n\n"
            )
            + f"SOURCED INSTRUMENT FACTS (US-equity look-through — trust THESE over labels):\n"
            f"{facts_lines}\n\n"
            f"CURRENT HOLDINGS (USD):\n{holdings_lines}\n\n"
            f"TAX-LOT / COST-BASIS COVERAGE FOR AFTER-TAX SELLS:\n"
            f"{tax_lot_lines}\n\n"
            f"STAGED SELL POLICY â€” current tranche only; reassess before the next:\n"
            f"{staged_sell_lines}\n\n"
            f"MARKET CONTEXT (current regime — factor into equity-vs-defensive & "
            f"US-vs-ex-US):\n{self._market_lines(p.get('policy_signals'))}\n\n"
            f"ARGOSY OUTCOME CALIBRATION (past graded calls; confidence/self-audit "
            f"context, not a mechanical gate):\n"
            f"{self._calibration_lines(p.get('decision_calibration'))}\n\n"
            + (
                "FRESH PER-CANDIDATE RESEARCH (live news/price on names you may "
                "buy — weigh it; if a name's news is materially negative, prefer "
                "another instrument for that sleeve):\n"
                f"{research_lines}\n\n"
                if research_lines
                else ""
            )
            + f"USER CONSTRAINTS: {p.get('user_constraints') or '(none)'}\n\n"
            + self._feedback_block(feedback)
            + "Author the AllocationProposal JSON now. Every buy is a plan-menu ticker, "
            "a FRESH search-origin discovery above, or a top-up of a current holding, "
            "with an honest `claimed_us_weight`; "
            "account for every dollar (deploy + reserve = new cash + verified net sells)."
        )
        return system, user

    @staticmethod
    def _market_lines(signals: dict | None) -> str:
        """Render the live market/macro regime for the prompt. Labels the fields
        we DO have (S&P level, VIX, USD/NIS, BoI rate, oil, CPI + a NVDA quote) and
        is explicit about freshness; absent series (valuations / real yields / credit
        spreads) are simply not shown, and the system prompt tells the author to say
        so rather than assume them."""
        if not signals:
            return "  (no live market context available — say so in the rationale)"
        snap = signals.get("snapshot") or {}
        labels = {
            "sp500": "S&P 500 level",
            "sp_vs_trend_pct": "S&P vs 200d MA %",
            "vix": "VIX (fear)",
            "fed_funds": "US Fed funds %",
            "ust10": "US 10y Treasury %",
            "real10": "US 10y REAL yield %",
            "breakeven10": "10y breakeven inflation %",
            "ig_spread": "IG credit spread (OAS) %",
            "hy_spread": "HY credit spread (OAS) %",
            "boi_rate": "BoI policy rate %",
            "usd_nis": "USD/NIS",
            "oil_wti": "Oil WTI",
            "cpi_yoy": "CPI YoY %",
        }
        lines = []
        for key, label in labels.items():
            if key in snap and snap[key]:
                lines.append(f"  - {label}: {snap[key]:,.2f}")
        nq = signals.get("nvda_quote") or {}
        if nq.get("price"):
            ok = nq.get("consistent")
            tag = "verified" if ok is True else ("INCONSISTENT" if ok is False else "unverified")
            lines.append(f"  - NVDA quote: ${nq['price']:,.2f} ({tag})")
        stale = " [STALE — treat with caution]" if signals.get("is_stale") else ""
        age = signals.get("as_of") or "unknown"
        header = f"  (as of {age}{stale})"
        return (header + "\n" + "\n".join(lines)) if lines else header

    @staticmethod
    def _calibration_lines(calibration: dict | None) -> str:
        if not calibration:
            return "  (no graded decision history available; uncalibrated)"
        lines: list[str] = []
        order_sheet = calibration.get("order_sheet") or {}
        if order_sheet:
            hit = order_sheet.get("hit_rate")
            hit_text = f"{float(hit):.1%}" if hit is not None else "not established"
            lines.append(
                "  - executable order sheets: "
                f"n={int(order_sheet.get('scored_predictions') or 0)}, "
                f"hit rate={hit_text}, "
                f"{'UNCALIBRATED' if order_sheet.get('sample_size_warning', True) else 'calibrated'}, "
                f"{'STALE' if order_sheet.get('is_stale', True) else 'active'}"
            )
        for row in calibration.get("sources") or []:
            hit = row.get("hit_rate")
            hit_text = f"{float(hit):.1%}" if hit is not None else "n/a"
            mean = row.get("mean_pnl_pct")
            mean_text = f"{float(mean):+.1%}" if mean is not None else "n/a"
            flags = []
            if row.get("sample_size_warning"):
                flags.append("small sample")
            if row.get("is_stale"):
                flags.append("stale")
            suffix = f" [{' / '.join(flags)}]" if flags else ""
            lines.append(
                f"  - {row.get('source')}: n={int(row.get('scored') or 0)}, "
                f"hit={hit_text}, mean signed return={mean_text}{suffix}"
            )
        for outcome in calibration.get("recent_verdict_outcomes") or []:
            move = outcome.get("price_move_pct")
            move_text = f", underlying {float(move):+.1f}%" if move is not None else ""
            lines.append(
                f"  - recent {outcome.get('ticker')} {outcome.get('verdict')}: "
                f"{outcome.get('grade')}{move_text}"
            )
        return "\n".join(lines) or "  (no graded decision history available; uncalibrated)"

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
            "fine:\n" + "\n".join(lines) + "\n\n"
        )


__all__ = ["DeploymentAuthorAgent"]
