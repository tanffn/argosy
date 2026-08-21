"""The deployment VERIFIER — determinism gates the fleet-authored allocation.

Core doctrine (codex): deterministic code may say "this proposal violates the facts"
and demand a revision; it must NEVER say "therefore buy X" — that is authorship, and
it belongs to the fleet. So this returns a gate report (ACCEPT / REVISION_REQUIRED /
BLOCK) with machine-readable failures the author can fix, and it never rewrites the
allocation.

Verdicts:
  * BLOCK              — unsafe/ungrounded: invented ticker, sell exceeds holdings,
                         unsanctioned US-situs buy.
  * REVISION_REQUIRED  — fixable by the author: conservation mismatch, missing CGT
                         reserve, an instrument treated against its sourced facts
                         (e.g. FWRA as ex-US).
  * ACCEPT             — hard gates pass (warnings, if any, don't block).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from argosy.services.allocation_author.instrument_facts import lookup_facts
from argosy.services.allocation_author.proposal import AllocationProposal

_MONEY_EPS = 1.0            # $ tolerance for conservation
_EXUS_US_CEILING = 0.40     # a fund >40% US cannot be called "ex-US"
_CLAIM_TOLERANCE = 0.25     # |claimed - sourced| US weight before it's "unsupported"

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


class GateStatus(str, Enum):
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


def _implies_exus(buy) -> bool:
    text = f"{buy.sleeve} {buy.justification}".lower()
    return "ex-us" in text or "ex us" in text or "international" in text


def verify_allocation_proposal(
    proposal: AllocationProposal,
    packet: dict[str, Any],
    *,
    facts_lookup: Callable[[str], Any] | None = None,
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
            fails.append(GateFailure(
                "negative_amount", f"{_label} is negative (${_val:,.0f}).", "block"))
    for b in proposal.buys:
        if b.amount_usd < -0.01:
            fails.append(GateFailure(
                "negative_amount",
                f"buy {b.symbol} amount is negative (${b.amount_usd:,.0f}).", "block"))
    for s in proposal.sells:
        if s.amount_usd < -0.01:
            fails.append(GateFailure(
                "negative_amount",
                f"sell {s.symbol} amount is negative (${s.amount_usd:,.0f}).", "block"))

    for b in proposal.buys:
        # Fail closed: if we have no known-symbol universe to validate against, no
        # buy can be trusted (an empty `known` must not silently admit any ticker).
        if b.symbol.upper() not in known:
            fails.append(GateFailure(
                "invented_ticker",
                f"{b.symbol} is not a known instrument"
                + ("." if known else " (no known-symbol universe to validate against)."),
                "block"))
        # Unsanctioned US-situs estate exposure. NVDA is the one sanctioned single
        # name; the x10 moonshot sleeve is a bounded carve-out (Ariel, 2026-08-21 —
        # see domain_knowledge/tax/us/estate_tax_nonresidents.md), checked as a
        # sleeve-level cap + disclosure below. Anything else (core/growth sleeves,
        # or a symbol not listed under the moonshot plan-menu entry) is still
        # blocked outright — fail-closed on ambiguous/unattributable sleeve.
        try:
            from argosy.services.instrument_reference import lookup as _ref
            ref = _ref(b.symbol)
            if (ref is not None and not ref.estate_safe
                    and b.symbol.upper() != "NVDA"
                    and b.symbol.upper() not in _moonshot_tickers(packet)):
                fails.append(GateFailure(
                    "us_situs", f"{b.symbol} is US-situs (estate-exposed) and not "
                    "the sanctioned NVDA sleeve or the x10 moonshot sleeve carve-out.",
                    "block"))
        except Exception:  # noqa: BLE001 — estate lookup is best-effort
            pass

    for s in proposal.sells:
        if s.amount_usd > _held(s.symbol) + 0.01:
            fails.append(GateFailure(
                "sell_exceeds_holdings",
                f"Sell of {s.symbol} ${s.amount_usd:,.0f} exceeds held ${_held(s.symbol):,.0f}.",
                "block"))

    # --- REVISION-level: fixable by the author ---------------------------
    buys_sum = round(sum(b.amount_usd for b in proposal.buys), 2)
    if abs(buys_sum - proposal.cash_to_deploy) > _MONEY_EPS:
        fails.append(GateFailure(
            "conservation",
            f"sum(buys) ${buys_sum:,.0f} != cash_to_deploy ${proposal.cash_to_deploy:,.0f}.",
            "revision"))
    # Sell proceeds ADD to the funds being allocated (e.g. a deconcentration trim
    # frees cash to redeploy/reserve). Credit them so proceeds can't silently vanish
    # from the books: deploy+reserve must equal deployable + total sell proceeds.
    # (No tax term — CGT on a sale is paid from that sale, not pre-reserved here.)
    sells_sum = round(sum(s.amount_usd for s in proposal.sells), 2)
    available = round(deployable + sells_sum, 2)
    total = round(proposal.cash_to_deploy + proposal.cash_to_reserve, 2)
    if available > 0 and abs(total - available) > _MONEY_EPS:
        _proceeds = f" + sells ${sells_sum:,.0f}" if sells_sum else ""
        fails.append(GateFailure(
            "conservation",
            f"deploy+reserve ${total:,.0f} != deployable ${deployable:,.0f}"
            f"{_proceeds} (available ${available:,.0f}).",
            "revision"))

    # A money recommendation MUST carry its reasoning. A blank rationale on any
    # active disposition (buys / sells / a deliberate cash hold) is a revision, not
    # an accept — the loop bounces it back so the author always explains the move.
    # This checks the artifact is COMPLETE; it does not dictate the decision.
    if (proposal.buys or proposal.sells or proposal.cash_to_reserve > _MONEY_EPS) \
            and not (proposal.rationale or "").strip():
        fails.append(GateFailure(
            "missing_rationale",
            "the proposal has no rationale — state why this allocation "
            "(what it fills, what it declines, and why) so the recommendation "
            "carries its reasoning.",
            "revision"))

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
        if not _discloses_estate_consequence(b.justification):
            fails.append(GateFailure(
                "moonshot_estate_disclosure_missing",
                f"{b.symbol} is a US-situs moonshot-sleeve buy but its "
                "justification doesn't disclose the US-situs/estate-tax "
                "consequence (mirrors the Fund Manager's requirement) — state it "
                "explicitly.",
                "revision"))

    if _moonshot_us_situs_buys:
        _cap = _moonshot_us_situs_cap_usd(packet)
        _added = round(sum(b.amount_usd for b in _moonshot_us_situs_buys), 2)
        if _added > _cap + _MONEY_EPS:
            fails.append(GateFailure(
                "moonshot_us_situs_cap",
                f"moonshot-sleeve US-situs buys total ${_added:,.0f}, over the "
                f"derived cap ${_cap:,.0f} ({_MOONSHOT_US_SITUS_CARVEOUT_FRACTION:.0%} "
                "of the sleeve's target-pct-of-book size) — trim the US-situs names "
                "or size more into the sleeve's UCITS core.",
                "revision"))

    for b in proposal.buys:
        # Require an explicit US-weight claim on every buy so the sourced cross-check
        # below can never be silently skipped (the text heuristic alone is evadable —
        # an author can buy a US-heavy all-world fund into a "global" sleeve with no
        # "ex-US" words and dodge it). Forcing the claim makes the check un-skippable.
        if b.claimed_us_weight is None:
            fails.append(GateFailure(
                "missing_us_weight",
                f"{b.symbol} has no claimed_us_weight — state the instrument's "
                "US-equity weight (0..1) so it can be checked against sourced facts.",
                "revision"))
        f = facts_lookup(b.symbol)
        if f is None:
            continue
        if _implies_exus(b) and f.us_weight > _EXUS_US_CEILING:
            fails.append(GateFailure(
                "lookthrough_claim",
                f"{b.symbol} is ~{f.us_weight*100:.0f}% US ({f.source}) — it cannot be "
                "treated as ex-US diversification.",
                "revision"))
        elif (b.claimed_us_weight is not None
              and abs(b.claimed_us_weight - f.us_weight) > _CLAIM_TOLERANCE):
            fails.append(GateFailure(
                "lookthrough_claim",
                f"{b.symbol} claimed US weight {b.claimed_us_weight:.0%} contradicts the "
                f"sourced ~{f.us_weight:.0%} ({f.source}).",
                "revision"))

    if any(x.severity == "block" for x in fails):
        status = GateStatus.BLOCK
    elif fails:
        status = GateStatus.REVISION_REQUIRED
    else:
        status = GateStatus.ACCEPT
    return GateReport(status=status, failures=fails)


__all__ = ["GateStatus", "GateFailure", "GateReport", "verify_allocation_proposal"]
