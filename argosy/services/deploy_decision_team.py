"""The deploy DECISION TEAM — judgment reviewed by judgment, not by a gate.

The author proposes an ``AllocationProposal``; a bounded set of BLIND reviewers
(one per lens) re-derive from the raw facts and object by judgment; the
orchestrator reconciles objections per ticker into a team decision. A buy the
team objects to is FLAGGED (surfaced to the client / bounced), not silently
shipped. Reviewers run fail-open — a dead reviewer degrades to "fewer eyes",
never blocks — and the team is bounded (one per lens), so it doesn't repeat the
unbounded-fleet timeout that got the team cut before.

Determinism is deliberately NOT here: this is the judgment layer. The only
deterministic checks (conservation, estate) live in the author's verifier as the
inviolable-arithmetic floor.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from argosy.logging import get_logger

log = get_logger(__name__)

DEFAULT_LENSES: tuple[str, ...] = ("concentration", "diversification", "prudence")


@dataclass
class TeamDecision:
    approved: list[Any] = field(default_factory=list)       # buys no reviewer objected to
    flagged: list[dict[str, Any]] = field(default_factory=list)  # {symbol, amount_usd, objections}
    reviews: list[Any] = field(default_factory=list)
    reviewers_ran: int = 0
    reviewers_expected: int = 0

    @property
    def all_clear(self) -> bool:
        return not self.flagged

    @property
    def degraded(self) -> bool:
        """True when fewer reviewers ran than expected — fewer eyes on the trade."""
        return self.reviewers_ran < self.reviewers_expected


def _enrich_facts_with_nvda(packet: dict[str, Any], extra_symbols: set[str] | None = None) -> dict[str, Any]:
    """Hand the reviewers the raw per-instrument NVDA look-through as ground truth,
    so a false 'diversifier' (e.g. R1GR) is refuted from FACTS, not world knowledge.
    Annotates existing facts AND adds a row for every plan-menu / proposed symbol
    that has a look-through entry but no fact yet (R1GR lives in the menu, not the
    sourced-facts table). Best-effort; leaves the packet unchanged on any failure."""
    try:
        from argosy.services.deployment_funnel.look_through import LOOKTHROUGH_MAP, _weight

        facts = [dict(f) for f in (packet.get("instrument_facts") or [])]
        seen = {f.get("symbol", "").upper() for f in facts}
        for f in facts:
            f["nvda_weight"] = _weight(f.get("symbol", ""), "nvda")
        # Collect every symbol the reviewers might judge: menu tickers + proposed buys.
        candidates: set[str] = set(extra_symbols or set())
        for m in packet.get("plan_menu") or []:
            for t in m.get("tickers") or []:
                candidates.add(str(t).upper())
        for sym in sorted(candidates):
            if sym in seen or sym not in LOOKTHROUGH_MAP:
                continue
            facts.append({
                "symbol": sym,
                "us_weight": _weight(sym, "us"),
                "nvda_weight": _weight(sym, "nvda"),
                "source": "lookthrough_map",
                "confidence": "table",
            })
        return {**packet, "instrument_facts": facts}
    except Exception as exc:  # noqa: BLE001 — enrichment is additive/best-effort
        log.warning("deploy_team.fact_enrich_failed", err=str(exc)[:120])
        return packet


def _default_review(lens: str, packet: dict[str, Any], buys: list[dict[str, Any]], *, user_id: str):
    """One reviewer call through the shared fleet-reliability envelope: the
    claude.exe exit-1 flake arrives in minutes-long bursts that outlive the
    agent's in-call sub-second retries (a reviewer died live on 2026-07-05 and
    only fail-open covered it). Long-backoff retries on a fresh agent + a hard
    timeout with process-tree kill; the team's fail-open stays the last resort."""
    from argosy.services.fleet_reliability import (
        DEPLOY_REVIEWER_CONFIG,
        call_reliably_sync,
    )

    def _attempt():
        from argosy.agents.deployment_reviewer import DeploymentReviewerAgent

        agent = DeploymentReviewerAgent(user_id=user_id)
        return agent.run_sync(lens=lens, packet=packet, buys=buys).output

    return call_reliably_sync(
        _attempt, scope="deploy_reviewers", config=DEPLOY_REVIEWER_CONFIG,
    )


def run_deploy_decision_team(
    packet: dict[str, Any],
    proposal: Any,
    *,
    lenses: tuple[str, ...] = DEFAULT_LENSES,
    review_fn: Callable[..., Any] | None = None,
    user_id: str = "ariel",
) -> TeamDecision:
    """Run the blind-reviewer team over the author's proposal and reconcile.

    ``review_fn(lens, packet, blind_buys, user_id=...)`` returns a
    ``DeploymentReviewOutput`` (injected for tests). Reviewers run fail-open.
    """
    review_fn = review_fn or _default_review
    _buy_syms = {b.symbol.upper() for b in (proposal.buys or [])}
    enriched = _enrich_facts_with_nvda(packet, extra_symbols=_buy_syms)

    # BLIND: reviewers see ticker/amount/sleeve only — never the author's rationale.
    blind_buys = [
        {"symbol": b.symbol, "amount_usd": b.amount_usd, "sleeve": getattr(b, "sleeve", "")}
        for b in (proposal.buys or [])
    ]

    reviews: list[Any] = []
    for lens in lenses:
        try:
            r = review_fn(lens, enriched, blind_buys, user_id=user_id)
        except Exception as exc:  # noqa: BLE001 — fail-open: a dead reviewer never blocks
            log.warning("deploy_team.reviewer_failed", lens=lens, err=str(exc)[:120])
            r = None
        if r is not None:
            reviews.append(r)

    objections_by_ticker: dict[str, list[dict[str, Any]]] = {}
    for r in reviews:
        for o in getattr(r, "objections", []) or []:
            objections_by_ticker.setdefault(o.ticker.upper(), []).append(
                {"lens": r.lens, "concern": o.concern, "severity": o.severity}
            )

    approved = [b for b in (proposal.buys or []) if b.symbol.upper() not in objections_by_ticker]
    flagged = [
        {
            "symbol": b.symbol,
            "amount_usd": b.amount_usd,
            "objections": objections_by_ticker[b.symbol.upper()],
        }
        for b in (proposal.buys or [])
        if b.symbol.upper() in objections_by_ticker
    ]
    return TeamDecision(
        approved=approved, flagged=flagged, reviews=reviews,
        reviewers_ran=len(reviews), reviewers_expected=len(lenses),
    )


__all__ = ["run_deploy_decision_team", "TeamDecision", "DEFAULT_LENSES"]
