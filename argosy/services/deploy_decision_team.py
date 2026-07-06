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


def _situs_facts(symbol: str) -> dict[str, Any]:
    """Per-symbol US-situs / domicile FACT from the curated instrument reference
    (estate classification), so reviewers re-derive estate exposure from
    incorporation facts — never from the author's claimed weights. A geographic
    'us_weight' of 0 does NOT mean non-US-situs (MELI: LatAm economics but
    Delaware-incorporated = US-situs). Empty dict when the symbol is uncurated."""
    from argosy.services.instrument_reference import estate_safe_for

    safe = estate_safe_for(symbol)
    if safe is None:
        return {}
    return {
        "us_situs": not safe,
        "domicile": "US" if not safe else "non-US (UCITS/IL)",
    }


def _enrich_facts_with_nvda(packet: dict[str, Any], extra_symbols: set[str] | None = None) -> dict[str, Any]:
    """Hand the reviewers the raw per-instrument NVDA look-through as ground truth,
    so a false 'diversifier' (e.g. R1GR) is refuted from FACTS, not world knowledge.
    Also stamps each fact with the instrument's US-situs / domicile (from the
    curated reference) so estate exposure is re-derived, never taken from the
    author's claims. Annotates existing facts AND adds a row for every plan-menu /
    proposed symbol that has a look-through or situs entry but no fact yet (R1GR
    lives in the menu, not the sourced-facts table). Best-effort; leaves the
    packet unchanged on any failure."""
    try:
        from argosy.services.deployment_funnel.look_through import LOOKTHROUGH_MAP, _weight

        facts = [dict(f) for f in (packet.get("instrument_facts") or [])]
        seen = {f.get("symbol", "").upper() for f in facts}
        for f in facts:
            f["nvda_weight"] = _weight(f.get("symbol", ""), "nvda")
            f.update(_situs_facts(f.get("symbol", "")))
        # Collect every symbol the reviewers might judge: menu tickers + proposed buys.
        candidates: set[str] = set(extra_symbols or set())
        for m in packet.get("plan_menu") or []:
            for t in m.get("tickers") or []:
                candidates.add(str(t).upper())
        for sym in sorted(candidates):
            if sym in seen:
                continue
            situs = _situs_facts(sym)
            if sym not in LOOKTHROUGH_MAP and not situs:
                continue
            row: dict[str, Any] = {
                "symbol": sym,
                "source": "lookthrough_map",
                "confidence": "table",
                **situs,
            }
            if sym in LOOKTHROUGH_MAP:
                row["us_weight"] = _weight(sym, "us")
                row["nvda_weight"] = _weight(sym, "nvda")
            facts.append(row)
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


_SEVERITY_BY_OBJECTION = {"block": "warning", "warn": "info"}


def write_team_flag_proposals(db: Any, user_id: str, decision: TeamDecision) -> int:
    """Surface each team-flagged buy as an open ActionProposal (the inbox sink) —
    the author proposed it, a blind reviewer objected, so the disagreement is
    the client's to decide. Idempotent per (user, symbol) via dedup_key; on a
    collision (an open flag already surfaced for the symbol) the OPEN row is
    REFRESHED IN PLACE (amount / objections / severity / surfaced_at) so the
    inbox always shows TODAY's flag, never a stale amount for a buy that no
    longer exists. Returns the number of rows written or refreshed."""
    import json
    from datetime import datetime, timedelta, timezone

    from sqlalchemy.exc import IntegrityError

    from argosy.state.models import ActionProposal

    now = datetime.now(timezone.utc)
    written = 0
    for f in decision.flagged:
        objections = f.get("objections") or []
        worst = "block" if any(o.get("severity") == "block" for o in objections) else "warn"
        lenses = ", ".join(dict.fromkeys(o.get("lens", "") for o in objections))
        concerns = "\n".join(
            f"- **{o.get('lens')}** ({o.get('severity')}): {o.get('concern')}"
            for o in objections
        )
        summary = (
            f"Deploy team flagged buying {f['symbol']} "
            f"(${f.get('amount_usd', 0):,.0f}) — {lenses} objection"
        )
        rationale_md = (
            "The deploy author proposed this buy; blind reviewers re-derived "
            "from the raw facts and objected:\n\n" + concerns +
            "\n\nThe buy was NOT executed — your call."
        )
        suggested_payload = json.dumps({
            "symbol": f["symbol"], "amount_usd": f.get("amount_usd"),
            "objections": objections,
        })
        severity = _SEVERITY_BY_OBJECTION.get(worst, "info")
        dedup_key = f"deploy_team_flag:{user_id}:{str(f['symbol']).upper()}"
        row = ActionProposal(
            user_id=user_id,
            summary=summary,
            rationale_md=rationale_md,
            suggested_payload=suggested_payload,
            severity=severity,
            surfaced_at=now,
            expires_at=now + timedelta(days=14),
            status="open",
            kind="deploy_team_flag",
            dedup_key=dedup_key,
            execution_state="proposed",
        )
        db.add(row)
        try:
            db.commit()
            written += 1
        except IntegrityError as exc:
            # A dedup collision means yesterday's OPEN flag holds the slot —
            # refresh it in place with TODAY's flag (keep the row id and
            # status='open') instead of skipping, so the inbox never shows a
            # stale amount/objection set. Log the actual error too: a
            # CHECK-constraint failure looked exactly like a dedup collision
            # here and silently killed this sink for a day (migration 0077
            # relaxed the kind CHECK).
            db.rollback()
            try:
                existing = (
                    db.query(ActionProposal)
                    .filter_by(dedup_key=dedup_key, status="open")
                    .first()
                )
            except Exception:  # noqa: BLE001 — fake/limited test DBs
                existing = None
            if existing is not None:
                existing.summary = summary
                existing.rationale_md = rationale_md
                existing.suggested_payload = suggested_payload
                existing.severity = severity
                existing.surfaced_at = now
                existing.expires_at = now + timedelta(days=14)
                db.commit()
                written += 1
                log.info(
                    "deploy_team.flag_refreshed",
                    symbol=f["symbol"], proposal_id=existing.id,
                )
            else:
                log.warning(
                    "deploy_team.flag_write_skipped",
                    symbol=f["symbol"], error=str(exc.orig)[:160],
                )
    if written:
        log.info("deploy_team.flags_surfaced", n=written)
    return written


def supersede_cleared_flags(
    db: Any, user_id: str, decision: TeamDecision, reviewed_symbols: set[str],
) -> int:
    """Close open deploy_team_flag rows for symbols the team RE-REVIEWED this
    run and did NOT flag (approved, or dropped from the proposal entirely).
    A resolved flag must DISAPPEAR from the client's checklist — the team
    cleared it, so leaving yesterday's objection open punts a settled question
    back to the client. Rows for symbols not reviewed this run are left alone
    (their flags may still be live concerns). Returns rows superseded."""
    from argosy.state.models import ActionProposal

    still_flagged = {str(f["symbol"]).upper() for f in decision.flagged}
    cleared = {s.upper() for s in reviewed_symbols} - still_flagged
    if not cleared:
        return 0
    superseded = 0
    try:
        open_rows = (
            db.query(ActionProposal)
            .filter_by(user_id=user_id, kind="deploy_team_flag", status="open")
            .all()
        )
        for row in open_rows:
            sym = (row.dedup_key or "").rsplit(":", 1)[-1].upper()
            if sym in cleared:
                row.status = "superseded"
                superseded += 1
                log.info(
                    "deploy_team.flag_superseded",
                    symbol=sym, proposal_id=row.id,
                    reason="re-reviewed this run and cleared",
                )
        if superseded:
            db.commit()
    except Exception as exc:  # noqa: BLE001 — cleanup is additive/best-effort
        db.rollback()
        log.warning("deploy_team.flag_supersede_failed", error=str(exc)[:160])
    return superseded


__all__ = [
    "run_deploy_decision_team",
    "supersede_cleared_flags",
    "write_team_flag_proposals",
    "TeamDecision",
    "DEFAULT_LENSES",
]
