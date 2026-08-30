"""The deployment VERIFIER — determinism gates the fleet-authored allocation.

Core doctrine (codex): deterministic code may say "this proposal violates the facts"
and demand a revision; it must NEVER say "therefore buy X" — that is authorship, and
it belongs to the fleet. So this returns a gate report (ACCEPT / REVISION_REQUIRED /
BLOCK) with machine-readable failures the author can fix, and it never rewrites the
allocation.

Verdicts:
  * BLOCK              — unsafe/ungrounded: invented ticker, sell exceeds holdings,
                         unsanctioned US-situs buy.
  * REVISION_REQUIRED  — fixable by the author: conservation mismatch, unresolved
                         after-tax sale proceeds, an instrument treated against sourced facts
                         (e.g. FWRA as ex-US).
  * ACCEPT             — hard gates pass (warnings, if any, don't block).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from argosy.services.after_tax import (
    SaleTaxPolicy,
    TaxLotInput,
    calculate_after_tax_sale,
)
from argosy.services.allocation_author.instrument_facts import lookup_facts
from argosy.services.allocation_author.proposal import AllocationProposal

_MONEY_EPS = 1.0  # $ tolerance for conservation
_CLAIM_TOLERANCE = 0.25  # |claimed - sourced| US weight before it's "unsupported"

# --- Ariel's ruling (2026-08-21): the x10 moonshot sleeve MAY buy US-situs -----
# names. See domain_knowledge/tax/us/estate_tax_nonresidents.md, "Sleeve carve-out
# for the x10 moonshot sleeve" — this is the authoritative record of the ruling and
# its conditions. Core/growth sleeves are UNCHANGED: still blocked (below).
#
# Sleeve attribution is NOT trusted from `Buy.sleeve` (an LLM-authored free-text
# field observed EMPTY on a live run — unreliable, and evadable even when filled).
# Instead it is derived deterministically from the plan menu the packet already
# carries: a plan_menu entry is the moonshot sleeve iff it carries the binding
# X10_SLEEVE_MANDATE text (packet_assembly keys this on
# ``sigma_class == HIGH_GROWTH_SIGMA_CLASS``), and sleeve membership is ticker
# membership in that entry's own `tickers` list. A buy whose symbol is not listed
# under a mandated sleeve is fail-closed to CORE — never treated as moonshot.
#
# Cap derivation (not invented): the sleeve's UCITS thematic core is, by
# construction, never US-situs (see high_potential_sleeve.py) — only its
# single-name carve-out can be. That carve-out is sized ~40% of the sleeve by the
# seed design's conviction-weight split ("~60% UCITS thematic core / ~40%
# single-name carve-out"). So the per-proposal ceiling on US-situs dollars added by
# the sleeve is that fraction of the sleeve's OWN steady-state target dollar size
# (target_pct/100 x book_usd, both already in the packet) — never a fixed dollar
# figure.
_MOONSHOT_US_SITUS_CARVEOUT_FRACTION = 0.40


def _moonshot_plan_menu_entries(packet: dict[str, Any]) -> list[dict[str, Any]]:
    """plan_menu entries carrying the binding X10 mandate — i.e. the moonshot
    sleeve. Empty if the packet has no plan_menu (fail-closed: no moonshot)."""
    return [e for e in (packet.get("plan_menu") or []) if e.get("mandate")]


def _moonshot_tickers(packet: dict[str, Any]) -> set[str]:
    """Tickers actually listed under the moonshot sleeve in the plan menu — the
    ONLY basis for sleeve attribution (never the author's free-text Buy.sleeve)."""
    out: set[str] = set()
    for entry in _moonshot_plan_menu_entries(packet):
        out |= {str(t).upper() for t in (entry.get("tickers") or [])}
    return out


def _moonshot_us_situs_cap_usd(packet: dict[str, Any]) -> float:
    """Derived (not invented) $ ceiling: carve-out fraction x sleeve target $ size.
    0.0 (nothing allowed) if the packet doesn't establish a moonshot sleeve/book."""
    book = float((packet.get("nvda") or {}).get("book_usd") or 0.0)
    if book <= 0:
        return 0.0
    total = 0.0
    for entry in _moonshot_plan_menu_entries(packet):
        total += book * float(entry.get("target_pct") or 0.0) / 100.0
    return total * _MOONSHOT_US_SITUS_CARVEOUT_FRACTION


def _discloses_estate_consequence(text: str) -> bool:
    """Mirrors what the Fund Manager already requires: the justification must
    name BOTH the US-situs fact and the estate-tax consequence, not just one."""
    t = (text or "").lower()
    has_situs = "us-situs" in t or "us situs" in t
    has_estate = "estate" in t
    return has_situs and has_estate


class GateStatus(StrEnum):
    ACCEPT = "ACCEPT"
    REVISION_REQUIRED = "REVISION_REQUIRED"
    BLOCK = "BLOCK"


@dataclass
class GateFailure:
    code: str
    detail: str
    severity: str  # "block" | "revision"


@dataclass
class GateReport:
    status: GateStatus
    failures: list[GateFailure] = field(default_factory=list)


def verify_allocation_proposal(
    proposal: AllocationProposal,
    packet: dict[str, Any],
    *,
    facts_lookup: Callable[[str], Any] | None = None,
    sale_resolver: Callable[[str, float], Any] | None = None,
) -> GateReport:
    """Gate a fleet-authored ``AllocationProposal`` against the decision packet's
    facts. Never mutates or re-authors the allocation."""
    facts_lookup = facts_lookup or lookup_facts
    fails: list[GateFailure] = []

    known = {s.upper() for s in (packet.get("known_symbols") or set())}
    holdings = packet.get("holdings") or {}
    deployable = float(packet.get("deployable_usd") or 0.0)

    def _held(sym: str) -> float:
        return float(holdings.get(sym, holdings.get(sym.upper(), 0.0)) or 0.0)

    horizon_values = packet.get("horizon_years") or [1, 5]
    max_horizon_years = int(horizon_values[-1])

    def _terminal_date_error(scenarios, *, start_date: date) -> str | None:
        dates = {s.terminal_date for s in scenarios if s.terminal_date is not None}
        if len(dates) != 1 or any(s.terminal_date is None for s in scenarios):
            return "every terminal scenario must use one exact terminal_date"
        terminal = next(iter(dates))
        days = (terminal - start_date).days
        if days <= 0 or days > int(max_horizon_years * 365.25) + 2:
            return (
                f"terminal_date {terminal} is outside the requested "
                f"{max_horizon_years}-year horizon"
            )
        return None

    # --- Discovery finalist explanation completeness ---------------------
    # The fleet authors the winner/loser judgment. Determinism only proves
    # that every fresh research-BUY finalist was explicitly adjudicated and
    # that copied rank/grade/freshness facts match the packet.
    discovery_rows = {
        str(row.get("ticker") or "").strip().upper(): row
        for row in (packet.get("discovery_candidates") or [])
        if str(row.get("ticker") or "").strip()
    }
    discovery_buy_amounts = {
        str(b.symbol or "").strip().upper(): float(b.amount_usd)
        for b in proposal.buys
        if str(b.symbol or "").strip().upper() in discovery_rows
    }
    discovery_buys = set(discovery_buy_amounts)
    research_buy_finalists = {
        ticker
        for ticker, row in discovery_rows.items()
        if str((row.get("fleet") or {}).get("verdict") or "").upper() == "BUY"
    }
    required_comparisons = research_buy_finalists | discovery_buys
    comparisons: dict[str, Any] = {}
    for comparison in proposal.candidate_comparisons:
        ticker = comparison.ticker.upper()
        if ticker in comparisons:
            fails.append(
                GateFailure(
                    "duplicate_candidate_comparison",
                    f"{ticker}: finalist appears more than once in candidate_comparisons.",
                    "revision",
                )
            )
            continue
        comparisons[ticker] = comparison
    missing_comparisons = sorted(required_comparisons - set(comparisons))
    if missing_comparisons:
        fails.append(
            GateFailure(
                "candidate_comparison_missing",
                "Explicit winner/loser rationale is missing for fresh discovery "
                f"finalists: {missing_comparisons}.",
                "revision",
            )
        )
    for ticker in sorted(required_comparisons & set(comparisons)):
        comparison = comparisons[ticker]
        row = discovery_rows[ticker]
        fleet = row.get("fleet") or {}
        expected_selection = "SELECTED" if ticker in discovery_buys else "NOT_SELECTED"
        if comparison.selection != expected_selection:
            fails.append(
                GateFailure(
                    "candidate_selection_mismatch",
                    f"{ticker}: comparison says {comparison.selection}, but the one-list "
                    f"allocation requires {expected_selection}.",
                    "revision",
                )
            )
        if comparison.radar_rank != row.get("rank"):
            fails.append(
                GateFailure(
                    "candidate_rank_mismatch",
                    f"{ticker}: copied radar rank does not match the fresh search row.",
                    "revision",
                )
            )
        if (
            comparison.radar_score is None
            or abs(float(comparison.radar_score) - float(row.get("score") or 0.0)) > 0.01
        ):
            fails.append(
                GateFailure(
                    "candidate_score_mismatch",
                    f"{ticker}: copied radar score does not match the fresh search row.",
                    "revision",
                )
            )
        expected_verdict = str(fleet.get("verdict") or "").upper() or None
        expected_conviction = str(fleet.get("conviction") or "").upper() or None
        if comparison.research_verdict != expected_verdict:
            fails.append(
                GateFailure(
                    "candidate_grade_mismatch",
                    f"{ticker}: copied research verdict does not match the fleet grade.",
                    "revision",
                )
            )
        if comparison.research_conviction != expected_conviction:
            fails.append(
                GateFailure(
                    "candidate_conviction_mismatch",
                    f"{ticker}: copied research conviction does not match the fleet grade.",
                    "revision",
                )
            )
        scenarios = comparison.outcome_scenarios
        if len(scenarios) < 3:
            fails.append(
                GateFailure(
                    "candidate_probabilities_missing",
                    f"{ticker}: compare every research-BUY finalist on at least three "
                    "probability-weighted terminal scenarios.",
                    "revision",
                )
            )
        else:
            probability_total = sum(s.probability_pct for s in scenarios)
            if abs(probability_total - 100.0) > 0.05:
                fails.append(
                    GateFailure(
                        "candidate_probability_mismatch",
                        f"{ticker}: finalist scenario probabilities sum to "
                        f"{probability_total:.2f}%, not 100%.",
                        "revision",
                    )
                )
            if not any(s.terminal_multiple <= 0.2 for s in scenarios):
                fails.append(
                    GateFailure(
                        "candidate_wipeout_missing",
                        f"{ticker}: finalist model omits an economic-wipeout case (<=0.2x).",
                        "revision",
                    )
                )
            terminal_error = _terminal_date_error(
                scenarios,
                start_date=comparison.evidence_fresh_as_of.date(),
            )
            if terminal_error:
                fails.append(
                    GateFailure(
                        "candidate_terminal_date_missing",
                        f"{ticker}: {terminal_error}.",
                        "revision",
                    )
                )
        if comparison.probability_confidence is None or not (
            comparison.probability_basis or ""
        ).strip():
            fails.append(
                GateFailure(
                    "candidate_probability_basis_missing",
                    f"{ticker}: finalist probability confidence and evidence/base-rate "
                    "basis are required.",
                    "revision",
                )
            )
        sizing_errors = comparison.sizing_structure_errors()
        if sizing_errors:
            fails.append(
                GateFailure(
                    "candidate_sizing_missing",
                    f"{ticker}: " + "; ".join(sizing_errors),
                    "revision",
                )
            )
        elif comparison.selection == "SELECTED" and abs(
            comparison.recommended_position_usd - discovery_buy_amounts.get(ticker, 0.0)
        ) > 1.0:
            fails.append(
                GateFailure(
                    "candidate_sizing_order_mismatch",
                    f"{ticker}: comparison recommends ${comparison.recommended_position_usd:,.0f} "
                    f"but the order is ${discovery_buy_amounts.get(ticker, 0.0):,.0f}.",
                    "revision",
                )
            )
        try:
            expected_fresh = datetime.fromisoformat(str(row.get("fresh_as_of")))
            if expected_fresh.tzinfo is None:
                expected_fresh = expected_fresh.replace(tzinfo=UTC)
            actual_fresh = comparison.evidence_fresh_as_of
            if actual_fresh.tzinfo is None:
                actual_fresh = actual_fresh.replace(tzinfo=UTC)
            if abs((actual_fresh - expected_fresh).total_seconds()) > 1:
                raise ValueError("timestamp differs")
        except (TypeError, ValueError):
            fails.append(
                GateFailure(
                    "candidate_freshness_mismatch",
                    f"{ticker}: copied evidence freshness does not match the search row.",
                    "revision",
                )
            )

    # --- BLOCK-level: unsafe / ungrounded ---------------------------------
    # Non-negativity is the load-bearing money guard. The conservation checks below
    # are pure equalities, so without this a negative reserve (or a negative buy leg)
    # could balance an over-deploy and still pass. Determinism must make an
    # over-deploy IMPOSSIBLE — this is that guarantee (belt-and-suspenders with the
    # schema's ge=0). BLOCK, not revision: it is unsafe, not a judgment tweak.
    for _label, _val in (
        ("cash_to_deploy", proposal.cash_to_deploy),
        ("cash_to_reserve", proposal.cash_to_reserve),
    ):
        if _val < -0.01:
            fails.append(
                GateFailure("negative_amount", f"{_label} is negative (${_val:,.0f}).", "block")
            )
    for b in proposal.buys:
        if b.amount_usd < -0.01:
            fails.append(
                GateFailure(
                    "negative_amount",
                    f"buy {b.symbol} amount is negative (${b.amount_usd:,.0f}).",
                    "block",
                )
            )
    for s in proposal.sells:
        if s.amount_usd < -0.01:
            fails.append(
                GateFailure(
                    "negative_amount",
                    f"sell {s.symbol} amount is negative (${s.amount_usd:,.0f}).",
                    "block",
                )
            )

    for b in proposal.buys:
        # Fail closed: if we have no known-symbol universe to validate against, no
        # buy can be trusted (an empty `known` must not silently admit any ticker).
        if b.symbol.upper() not in known:
            fails.append(
                GateFailure(
                    "invented_ticker",
                    f"{b.symbol} is not a known instrument"
                    + ("." if known else " (no known-symbol universe to validate against)."),
                    "block",
                )
            )
        # Unsanctioned US-situs estate exposure. NVDA is the one sanctioned single
        # name; the x10 moonshot sleeve is a bounded carve-out (Ariel, 2026-08-21 —
        # see domain_knowledge/tax/us/estate_tax_nonresidents.md), checked as a
        # sleeve-level cap + disclosure below. Anything else (core/growth sleeves,
        # or a symbol not listed under the moonshot plan-menu entry) is still
        # blocked outright — fail-closed on ambiguous/unattributable sleeve.
        try:
            from argosy.services.instrument_reference import lookup as _ref

            ref = _ref(b.symbol)
            if (
                ref is not None
                and not ref.estate_safe
                and b.symbol.upper() != "NVDA"
                and b.symbol.upper() not in _moonshot_tickers(packet)
            ):
                fails.append(
                    GateFailure(
                        "us_situs",
                        f"{b.symbol} is US-situs (estate-exposed) and not "
                        "the sanctioned NVDA sleeve or the x10 moonshot sleeve carve-out.",
                        "block",
                    )
                )
        except Exception:  # noqa: BLE001 — estate lookup is best-effort
            pass

    staged_policies = {
        str(symbol).upper(): policy
        for symbol, policy in (packet.get("staged_sell_policies") or {}).items()
    }
    for s in proposal.sells:
        if not bool(packet.get("allow_sells", True)):
            fails.append(
                GateFailure(
                    "sell_forbidden_by_user",
                    f"{s.symbol}: the user explicitly prohibited sells for this run.",
                    "block",
                )
            )
        if s.amount_usd > _held(s.symbol) + 0.01:
            fails.append(
                GateFailure(
                    "sell_exceeds_holdings",
                    f"Sell of {s.symbol} ${s.amount_usd:,.0f} exceeds held ${_held(s.symbol):,.0f}.",
                    "block",
                )
            )
        staged = staged_policies.get(s.symbol.strip().upper())
        if staged is not None:
            if staged.get("status") != "actionable":
                fails.append(
                    GateFailure(
                        "staged_sell_not_actionable",
                        f"{s.symbol}: staged sale is blocked: "
                        + "; ".join(staged.get("failures") or ["policy unavailable"]),
                        "revision",
                    )
                )
                continue
            maximum = float(staged.get("maximum_current_tranche_usd") or 0.0)
            if maximum <= 0 or s.amount_usd > maximum + _MONEY_EPS:
                fails.append(
                    GateFailure(
                        "staged_sell_exceeds_current_tranche",
                        f"{s.symbol}: current sell ${s.amount_usd:,.0f} exceeds the "
                        f"priced current-clip ceiling ${maximum:,.0f}; the rest "
                        "requires a later reassessment.",
                        "revision",
                    )
                )
            if s.execution_style != "staged_tranche":
                fails.append(
                    GateFailure(
                        "staged_sell_metadata_missing",
                        f"{s.symbol}: set execution_style=staged_tranche; this is "
                        "one executable glide clip, not the full waypoint.",
                        "revision",
                    )
                )
            if s.execute_by is None or s.next_review_date is None:
                fails.append(
                    GateFailure(
                        "staged_sell_dates_missing",
                        f"{s.symbol}: execute_by and next_review_date are required.",
                        "revision",
                    )
                )
            else:
                policy_as_of = date.fromisoformat(str(staged["as_of"])[:10])
                deadline = date.fromisoformat(
                    str(staged["execute_no_later_than"])[:10]
                )
                if s.execute_by < policy_as_of or s.execute_by > deadline:
                    fails.append(
                        GateFailure(
                            "staged_sell_date_outside_window",
                            f"{s.symbol}: execute_by {s.execute_by} must be between "
                            f"{policy_as_of} and the current-clip deadline {deadline}.",
                            "revision",
                        )
                    )
                if (
                    s.next_review_date < s.execute_by
                    or s.next_review_date > deadline
                ):
                    fails.append(
                        GateFailure(
                            "staged_sell_review_date_invalid",
                            f"{s.symbol}: next_review_date must be on/after this "
                            f"clip and no later than {deadline}.",
                            "revision",
                        )
                    )
            if not (s.tranche_reason or "").strip():
                fails.append(
                    GateFailure(
                        "staged_sell_reason_missing",
                        f"{s.symbol}: explain why this current clip size and timing.",
                        "revision",
                    )
                )

    # --- REVISION-level: fixable by the author ---------------------------
    buys_sum = round(sum(b.amount_usd for b in proposal.buys), 2)
    if abs(buys_sum - proposal.cash_to_deploy) > _MONEY_EPS:
        fails.append(
            GateFailure(
                "conservation",
                f"sum(buys) ${buys_sum:,.0f} != cash_to_deploy ${proposal.cash_to_deploy:,.0f}.",
                "revision",
            )
        )
    # A sale contributes only independently resolved NET buying power. Gross
    # proceeds are never credited to conservation and tax is paid from the sale.
    sells_sum = round(sum(s.amount_usd for s in proposal.sells), 2)
    net_sells = 0.0
    sale_inputs = packet.get("sale_tax_inputs") or {}
    for sell in proposal.sells:
        if sale_resolver is not None:
            try:
                priced = sale_resolver(sell.symbol, float(sell.amount_usd))
            except Exception as exc:  # noqa: BLE001 - provider must fail loud
                fails.append(
                    GateFailure(
                        "sell_tax_unresolved",
                        f"{sell.symbol} after-tax provider failed: {exc}",
                        "revision",
                    )
                )
                continue
            if not priced.eligible or priced.net_fundable_usd is None:
                fails.append(
                    GateFailure(
                        "sell_tax_unresolved",
                        f"{sell.symbol} after-tax inputs are not actionable: "
                        + "; ".join(priced.failures),
                        "revision",
                    )
                )
                continue
            net_sells += priced.net_fundable_usd
            continue
        raw = sale_inputs.get(sell.symbol) or sale_inputs.get(sell.symbol.upper())
        if not raw:
            fails.append(
                GateFailure(
                    "sell_tax_unresolved",
                    f"{sell.symbol} has no verified lot/FX/friction inputs; gross "
                    "proceeds cannot fund buys.",
                    "revision",
                )
            )
            continue
        try:
            raw_as_of = raw.get("as_of")
            as_of = (
                datetime.fromisoformat(str(raw_as_of).replace("Z", "+00:00"))
                if raw_as_of
                else datetime.now().astimezone()
            )
            priced = calculate_after_tax_sale(
                gross_proceeds_usd=float(sell.amount_usd),
                current_price_usd=float(raw.get("current_price_usd") or 0.0),
                lots=[TaxLotInput(**lot) for lot in (raw.get("lots") or [])],
                policy=SaleTaxPolicy(**(raw.get("policy") or {})),
                as_of=as_of,
                friction_usd=raw.get("friction_usd"),
            )
        except Exception as exc:  # noqa: BLE001 - malformed input is fail-loud
            fails.append(
                GateFailure(
                    "sell_tax_unresolved",
                    f"{sell.symbol} after-tax calculation failed: {exc}",
                    "revision",
                )
            )
            continue
        if not priced.eligible or priced.net_fundable_usd is None:
            fails.append(
                GateFailure(
                    "sell_tax_unresolved",
                    f"{sell.symbol} after-tax inputs are not actionable: "
                    + "; ".join(priced.failures),
                    "revision",
                )
            )
            continue
        net_sells += priced.net_fundable_usd

    available = round(deployable + net_sells, 2)
    total = round(proposal.cash_to_deploy + proposal.cash_to_reserve, 2)
    if available > 0 and abs(total - available) > _MONEY_EPS:
        _proceeds = f" + net sells ${net_sells:,.0f} (gross ${sells_sum:,.0f})" if sells_sum else ""
        fails.append(
            GateFailure(
                "conservation",
                f"deploy+reserve ${total:,.0f} != deployable ${deployable:,.0f}"
                f"{_proceeds} (available ${available:,.0f}).",
                "revision",
            )
        )

    # A money recommendation MUST carry its reasoning. A blank rationale on any
    # active disposition (buys / sells / a deliberate cash hold) is a revision, not
    # an accept — the loop bounces it back so the author always explains the move.
    # This checks the artifact is COMPLETE; it does not dictate the decision.
    if (proposal.buys or proposal.sells or proposal.cash_to_reserve > _MONEY_EPS) and not (
        proposal.rationale or ""
    ).strip():
        fails.append(
            GateFailure(
                "missing_rationale",
                "the proposal has no rationale — state why this allocation "
                "(what it fills, what it declines, and why) so the recommendation "
                "carries its reasoning.",
                "revision",
            )
        )

    # --- Moonshot-sleeve US-situs buys: disclosure + derived cap (revision) ---
    # These are the buys the BLOCK-level loop above let through under the carve-out
    # (US-situs, not NVDA, ticker attributed to the moonshot sleeve). Both checks
    # are fixable by the author (add the disclosure sentence / trim the size), so
    # they are REVISION_REQUIRED, not BLOCK — consistent with this module's
    # doctrine that BLOCK is reserved for unsafe/ungrounded, not judgment tweaks.
    _moonshot_syms = _moonshot_tickers(packet)
    _moonshot_us_situs_buys = []
    for b in proposal.buys:
        if b.symbol.upper() == "NVDA" or b.symbol.upper() not in _moonshot_syms:
            continue
        try:
            from argosy.services.instrument_reference import lookup as _ref2

            _ref = _ref2(b.symbol)
        except Exception:  # noqa: BLE001 — best-effort
            _ref = None
        if _ref is not None and not _ref.estate_safe:
            _moonshot_us_situs_buys.append(b)

    for b in _moonshot_us_situs_buys:
        intent = getattr(b, "order_intent", None)
        acknowledged = bool(
            getattr(intent, "acknowledges_us_situs_estate_cost", False)
        )
        if not acknowledged and not _discloses_estate_consequence(b.justification):
            fails.append(
                GateFailure(
                    "moonshot_estate_disclosure_missing",
                    f"{b.symbol} is a US-situs moonshot-sleeve buy but its "
                    "justification doesn't disclose the US-situs/estate-tax "
                    "consequence (mirrors the Fund Manager's requirement) — state it "
                    "explicitly.",
                    "revision",
                )
            )

    if _moonshot_us_situs_buys:
        _cap = _moonshot_us_situs_cap_usd(packet)
        _added = round(sum(b.amount_usd for b in _moonshot_us_situs_buys), 2)
        if _added > _cap + _MONEY_EPS:
            fails.append(
                GateFailure(
                    "moonshot_us_situs_cap",
                    f"moonshot-sleeve US-situs buys total ${_added:,.0f}, over the "
                    f"derived cap ${_cap:,.0f} ({_MOONSHOT_US_SITUS_CARVEOUT_FRACTION:.0%} "
                    "of the sleeve's target-pct-of-book size) — trim the US-situs names "
                    "or size more into the sleeve's UCITS core.",
                    "revision",
                )
            )

    for b in proposal.buys:
        # Require an explicit US-weight claim on every buy so the sourced
        # cross-check cannot be skipped. Free-text substring classification was
        # removed: prose such as "rather than ex-US" was falsely treated as an
        # ex-US claim. The structured numeric claim is the deterministic fact seam.
        if b.claimed_us_weight is None:
            fails.append(
                GateFailure(
                    "missing_us_weight",
                    f"{b.symbol} has no claimed_us_weight — state the instrument's "
                    "US-equity weight (0..1) so it can be checked against sourced facts.",
                    "revision",
                )
            )
        f = facts_lookup(b.symbol)
        if f is None:
            continue
        if (
            b.claimed_us_weight is not None
            and abs(b.claimed_us_weight - f.us_weight) > _CLAIM_TOLERANCE
        ):
            fails.append(
                GateFailure(
                    "lookthrough_claim",
                    f"{b.symbol} claimed US weight {b.claimed_us_weight:.0%} contradicts the "
                    f"sourced ~{f.us_weight:.0%} ({f.source}).",
                    "revision",
                )
            )

    # An allocation amount is not yet an order.  The author must supply the
    # judgment fields that deterministic code cannot invent; the downstream
    # order-sheet builder resolves only arithmetic/live facts (price, shares,
    # venue, taxes and priced constraints).
    for side, rows in (("buy", proposal.buys), ("sell", proposal.sells)):
        for row in rows:
            if row.order_intent is None:
                fails.append(
                    GateFailure(
                        "missing_order_intent",
                        f"{side} {row.symbol} has no thesis/falsifier/dated catalyst/"
                        "outcome clock; author the judgment metadata required by the "
                        "first-class order sheet.",
                        "revision",
                    )
                )
                continue
            intent = row.order_intent
            if side == "buy" and intent.thesis_type.value == "convexity":
                scenarios = intent.outcome_scenarios
                if len(scenarios) < 3:
                    fails.append(
                        GateFailure(
                            "convexity_probabilities_missing",
                            f"{row.symbol}: author at least three probability-weighted "
                            "terminal scenarios, including economic wipeout and the upside case.",
                            "revision",
                        )
                    )
                else:
                    probability_total = sum(s.probability_pct for s in scenarios)
                    if abs(probability_total - 100.0) > 0.05:
                        fails.append(
                            GateFailure(
                                "scenario_probability_mismatch",
                                f"{row.symbol}: scenario probabilities sum to "
                                f"{probability_total:.2f}%, not 100%.",
                                "revision",
                            )
                        )
                    if not any(s.terminal_multiple <= 0.2 for s in scenarios):
                        fails.append(
                            GateFailure(
                                "wipeout_scenario_missing",
                                f"{row.symbol}: include an economic-wipeout scenario at <=0.2x.",
                                "revision",
                            )
                        )
                    terminal_error = _terminal_date_error(
                        scenarios,
                        start_date=datetime.now(UTC).date(),
                    )
                    if terminal_error:
                        fails.append(
                            GateFailure(
                                "scenario_terminal_date_missing",
                                f"{row.symbol}: {terminal_error}.",
                                "revision",
                            )
                        )
                    upside = intent.expected_upside_multiple or 0.0
                    if upside and max(s.terminal_multiple for s in scenarios) < upside:
                        fails.append(
                            GateFailure(
                                "upside_scenario_missing",
                                f"{row.symbol}: no probability scenario reaches the authored "
                                f"{upside:g}x upside case.",
                                "revision",
                            )
                        )
                if intent.probability_confidence is None or not (
                    intent.probability_basis or ""
                ).strip():
                    fails.append(
                        GateFailure(
                            "probability_basis_missing",
                            f"{row.symbol}: state LOW/MED/HIGH probability confidence and "
                            "the evidence/base-rate basis used for sizing.",
                            "revision",
                        )
                    )

    # The owner's sizing rule belongs in the author/revision loop, not only at
    # the final order-sheet boundary.  Otherwise the author can pass review with
    # a spray of economically immaterial ETF fragments and the real command fails
    # only after its revision budget is gone.  This is arithmetic over the
    # author's own thesis declaration; it does not choose or substitute a ticker.
    book_usd = float((packet.get("nvda") or {}).get("book_usd") or 0.0)
    if book_usd <= 0:
        # Pure/unit callers may omit the concentration block.  Deployable cash is
        # part of the post-funding book, matching build_gate_inputs in production.
        book_usd = sum(float(v or 0.0) for v in holdings.values()) + deployable
    # Tax/friction leave the portfolio when a sale funds the switch.
    post_trade_book_usd = max(0.0, book_usd - max(0.0, sells_sum - net_sells))
    if post_trade_book_usd > 0:
        for buy in proposal.buys:
            intent = buy.order_intent
            if intent is None:
                continue
            post_value = _held(buy.symbol) + float(buy.amount_usd)
            post_weight_pct = 100.0 * post_value / post_trade_book_usd
            if post_weight_pct >= 1.0:
                continue
            if intent.thesis_type.value != "convexity":
                fails.append(
                    GateFailure(
                        "small_position_without_convexity",
                        f"{buy.symbol}: post-trade position is only "
                        f"{post_weight_pct:.2f}% of the book; consolidate it or "
                        "author a genuine convexity thesis.",
                        "revision",
                    )
                )
            if (intent.expected_upside_multiple or 0.0) < 5.0:
                fails.append(
                    GateFailure(
                        "small_position_insufficient_asymmetry",
                        f"{buy.symbol}: a sub-1% slot requires at least a 5x "
                        "authored upside case; consolidate or resize it.",
                        "revision",
                    )
                )

    if any(x.severity == "block" for x in fails):
        status = GateStatus.BLOCK
    elif fails:
        status = GateStatus.REVISION_REQUIRED
    else:
        status = GateStatus.ACCEPT
    return GateReport(status=status, failures=fails)


__all__ = ["GateStatus", "GateFailure", "GateReport", "verify_allocation_proposal"]
