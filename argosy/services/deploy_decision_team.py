"""The deploy DECISION TEAM — judgment reviewed by judgment, not by a gate.

The author proposes an ``AllocationProposal``; a bounded set of BLIND reviewers
(one per lens) re-derive from the raw facts and object by judgment; the
orchestrator reconciles objections per ticker into a team decision. A buy the
team objects to is FLAGGED (surfaced to the client / bounced), not silently
shipped. A dead reviewer is recorded as degraded; the money-path caller refuses
to validate an allocation with a missing lens. The team is bounded (one per
lens), so it doesn't repeat the unbounded-fleet timeout that got the team cut.

Determinism is deliberately NOT here: this is the judgment layer. The only
deterministic checks (conservation, estate) live in the author's verifier as the
inviolable-arithmetic floor.
"""
from __future__ import annotations

import concurrent.futures
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

from argosy.logging import get_logger

log = get_logger(__name__)

DEFAULT_LENSES: tuple[str, ...] = (
    "concentration", "diversification", "prudence", "candidate_selection", "sizing",
)


def _canonical_plan_sleeve(
    packet: dict[str, Any],
    *,
    symbol: str,
    authored_sleeve: str,
) -> str:
    """Carry an unambiguous plan sleeve across the blind-review seam.

    Reviewers deliberately do not see the author's justification, so the
    structured sleeve field is load-bearing. The plan menu is authoritative for
    membership: fill an omitted or miscased label only when the ticker maps to
    exactly one sleeve. Ambiguous membership remains unattributed rather than
    being guessed by deterministic code.
    """

    wanted = symbol.strip().upper()
    candidates: list[str] = []
    for entry in packet.get("plan_menu") or []:
        tickers = {
            str(ticker).strip().upper()
            for ticker in (entry.get("tickers") or [])
        }
        sleeve = str(entry.get("sleeve") or "").strip()
        if wanted in tickers and sleeve and sleeve not in candidates:
            candidates.append(sleeve)

    stated = (authored_sleeve or "").strip()
    for candidate in candidates:
        if stated.casefold() == candidate.casefold():
            return candidate
    if len(candidates) == 1:
        return candidates[0]
    return stated or "unattributed"


@dataclass
class TeamDecision:
    approved: list[Any] = field(default_factory=list)       # buys no reviewer objected to
    flagged: list[dict[str, Any]] = field(default_factory=list)  # {symbol, amount_usd, objections}
    reviews: list[Any] = field(default_factory=list)
    reviewers_ran: int = 0
    reviewers_expected: int = 0

    @property
    def all_clear(self) -> bool:
        """True only when no reviewer asks to change the executable order."""
        return not self.material_flagged

    @property
    def material_flagged(self) -> list[dict[str, Any]]:
        """Objections whose judgment changes ticker, amount, inclusion, or action."""
        return [
            item
            for item in self.flagged
            if any(
                objection.get("impact", "advisory_only") != "advisory_only"
                or objection.get("severity") == "block"
                for objection in item.get("objections", [])
            )
        ]

    @property
    def blocking_flagged(self) -> list[dict[str, Any]]:
        """Compatibility alias: every action-changing dissent blocks one voice."""
        return self.material_flagged

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
        from argosy.agents.base import AgentReport
        from argosy.agents.deployment_reviewer import DeploymentReviewerAgent
        from argosy.services.agent_report_persistence import persist_agent_report_sync
        from argosy.services.allocation_author.reliable import packet_hash

        agent = DeploymentReviewerAgent(user_id=user_id)
        decision_id = f"deploy-{packet_hash(packet)[:24]}"
        report = agent.run_sync(
            lens=lens,
            packet=packet,
            buys=buys,
            decision_id=decision_id,
        )
        if isinstance(report, AgentReport):
            persist_agent_report_sync(report, decision_id=decision_id)
        return report.output

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
    ``DeploymentReviewOutput`` (injected for tests). Individual failures are
    captured in the result; the money path treats ``degraded`` as incomplete.
    """
    review_fn = review_fn or _default_review
    _buy_syms = {b.symbol.upper() for b in (proposal.buys or [])}
    enriched = _enrich_facts_with_nvda(packet, extra_symbols=_buy_syms)

    # Blind does not mean economically incomplete. Reviewers get the proposed
    # actions and already-verified conservation totals, but never the author's
    # rationale. That allows independent SELL/TRIM adjudication and prevents an
    # issuer's runway ("funded optionality") being mistaken for investor cash.
    proposed_sells = [
        {"symbol": s.symbol, "amount_usd": float(s.amount_usd)}
        for s in (getattr(proposal, "sells", None) or [])
    ]
    # Stay blind to the author's reasoning, but carry a receipt that each
    # finalist was explicitly evaluated. Otherwise a reviewer can mistake a
    # documented NOT_SELECTED decision for an unexamined omission and invent a
    # sale merely to fund it.
    candidate_dispositions = [
        {
            "ticker": str(getattr(row, "ticker", "")).upper(),
            "selection": str(getattr(row, "selection", "")),
            "recommended_position_usd": getattr(
                row, "recommended_position_usd", None
            ),
        }
        for row in (getattr(proposal, "candidate_comparisons", None) or [])
        if str(getattr(row, "ticker", "")).strip()
    ]
    deployable = float(enriched.get("deployable_usd") or 0.0)
    proposed_buys_usd = sum(
        float(b.amount_usd) for b in (getattr(proposal, "buys", None) or [])
    )
    proposed_reserve_usd = float(
        getattr(proposal, "cash_to_reserve", 0.0) or 0.0
    )
    verified_net_sells = max(
        0.0,
        float(getattr(proposal, "cash_to_deploy", 0.0) or 0.0)
        + proposed_reserve_usd
        - deployable,
    )
    enriched = {
        **enriched,
        "proposed_sells": proposed_sells,
        "author_candidate_dispositions": candidate_dispositions,
        "review_funding": {
            "deployable_usd": deployable,
            "verified_net_sell_proceeds_usd": verified_net_sells,
            "proposed_buys_usd": proposed_buys_usd,
            "proposed_reserve_usd": proposed_reserve_usd,
            "arithmetic_verified": True,
        },
    }

    # BLIND: reviewers see ticker/amount/sleeve only — never the author's rationale.
    # Preserve blindness while carrying the authoritative plan classification
    # over the seam. Updating this structural field also makes the accepted
    # proposal and UI tell the same sleeve story the reviewers judged.
    for buy in proposal.buys or []:
        buy.sleeve = _canonical_plan_sleeve(
            enriched,
            symbol=buy.symbol,
            authored_sleeve=getattr(buy, "sleeve", ""),
        )
    blind_buys = [
        {"symbol": b.symbol, "amount_usd": b.amount_usd, "sleeve": getattr(b, "sleeve", "")}
        for b in (proposal.buys or [])
    ]

    def _run_one(lens: str) -> tuple[str, Any | None]:
        try:
            review = review_fn(lens, enriched, blind_buys, user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("deploy_team.reviewer_failed", lens=lens, err=str(exc)[:120])
            review = None
        return lens, review

    # The lenses are independent and blind. Bound latency to the slowest lens,
    # rather than serially adding three model-call durations.
    by_lens: dict[str, Any] = {}
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, len(lenses)),
        thread_name_prefix="deploy-review",
    ) as executor:
        futures = [executor.submit(_run_one, lens) for lens in lenses]
        for future in concurrent.futures.as_completed(futures):
            lens, review = future.result()
            if review is not None:
                by_lens[lens] = review
    reviews = [by_lens[lens] for lens in lenses if lens in by_lens]

    objections_by_ticker: dict[str, list[dict[str, Any]]] = {}
    for r in reviews:
        for o in getattr(r, "objections", []) or []:
            objections_by_ticker.setdefault(o.ticker.upper(), []).append(
                {
                    "lens": r.lens,
                    "concern": o.concern,
                    "severity": o.severity,
                    "impact": o.impact,
                    "recommended_amount_usd": o.recommended_amount_usd,
                    "recommended_ticker": o.recommended_ticker,
                }
            )

    material_tickers = {
        ticker
        for ticker, objections in objections_by_ticker.items()
        if any(
            objection.get("impact", "advisory_only") != "advisory_only"
            or objection.get("severity") == "block"
            for objection in objections
        )
    }
    # "Approved" means shippable: advisory-only notes stay in the audit trail;
    # any action-changing judgment removes the proposed buy from the one voice.
    approved = [
        b for b in (proposal.buys or []) if b.symbol.upper() not in material_tickers
    ]
    amounts = {b.symbol.upper(): float(b.amount_usd) for b in (proposal.buys or [])}
    amounts.update(
        {
            s.symbol.upper(): float(s.amount_usd)
            for s in (getattr(proposal, "sells", None) or [])
        }
    )
    flagged = [
        {
            "symbol": ticker,
            "amount_usd": amounts.get(ticker, 0.0),
            "proposed": ticker in amounts,
            "objections": objections,
        }
        for ticker, objections in sorted(objections_by_ticker.items())
    ]
    return TeamDecision(
        approved=approved, flagged=flagged, reviews=reviews,
        reviewers_ran=len(reviews), reviewers_expected=len(lenses),
    )


def build_review_resolution(history: list[TeamDecision]):
    """Project bounded review rounds into the durable order-sheet audit record."""
    from argosy.services.order_sheet import (
        ReviewObjectionRecord,
        ReviewResolution,
    )

    if not history:
        return None
    final = history[-1]
    records: list[ReviewObjectionRecord] = []
    resolved = 0
    for round_index, decision in enumerate(history, start=1):
        is_final = round_index == len(history)
        for item in decision.flagged:
            for objection in item.get("objections", []):
                material = (
                    objection.get("impact", "advisory_only") != "advisory_only"
                    or objection.get("severity") == "block"
                )
                if material and is_final:
                    status = "unresolved"
                elif material:
                    status = "resolved_by_re_review"
                    resolved += 1
                else:
                    status = "advisory"
                records.append(
                    ReviewObjectionRecord(
                        round=round_index,
                        lens=objection.get("lens") or "unknown",
                        ticker=str(item.get("symbol") or "UNKNOWN").upper(),
                        concern=objection.get("concern") or "unspecified objection",
                        severity=objection.get("severity") or "warn",
                        impact=objection.get("impact") or (
                            "rejects_trade"
                            if objection.get("severity") == "block"
                            else "advisory_only"
                        ),
                        proposed_amount_usd=float(item.get("amount_usd") or 0.0),
                        recommended_amount_usd=objection.get("recommended_amount_usd"),
                        recommended_ticker=objection.get("recommended_ticker"),
                        status=status,
                    )
                )
    one_voice = not final.degraded and final.all_clear
    summary = (
        f"{final.reviewers_ran}/{final.reviewers_expected} reviewers completed; "
        f"{resolved} material disagreement(s) resolved across {len(history)} round(s)."
        if one_voice
        else (
            f"Review incomplete or unresolved after {len(history)} round(s); "
            "no executable one voice."
        )
    )
    return ReviewResolution(
        rounds=len(history),
        reviewers_ran=final.reviewers_ran,
        reviewers_expected=final.reviewers_expected,
        one_voice=one_voice,
        summary=summary,
        objections=records,
    )


_SEVERITY_BY_OBJECTION = {"block": "warning", "warn": "info"}


def write_team_flag_proposals(db: Any, user_id: str, decision: TeamDecision) -> int:
    """Surface each stop-level team objection as an open ActionProposal.

    Advisory notes belong on the accepted run's review record; turning them into
    a separate "your call" proposal creates a second, contradictory voice.
    Blocking disagreements remain client-visible and idempotent per (user,
    symbol) via dedup_key; on a
    collision (an open flag already surfaced for the symbol) the OPEN row is
    REFRESHED IN PLACE (amount / objections / severity / surfaced_at) so the
    inbox always shows TODAY's flag, never a stale amount for a buy that no
    longer exists. Returns the number of rows written or refreshed."""
    import json
    from datetime import datetime, timedelta

    from sqlalchemy.exc import IntegrityError

    from argosy.state.models import ActionProposal

    now = datetime.now(UTC)
    written = 0
    for f in decision.material_flagged:
        objections = f.get("objections") or []
        worst = "block" if any(
            o.get("severity") == "block" or o.get("impact") != "advisory_only"
            for o in objections
        ) else "warn"
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
    """Make the inbox reflect only stop-level objections from the current run.

    A final run supersedes old symbols dropped during re-authoring and current
    warn-only notes alike. The advisory history remains in the team review; an
    open action card is reserved for a current blocking disagreement. The
    ``reviewed_symbols`` parameter is retained for call compatibility and audit
    clarity, but the current decision is the source of truth. Returns rows
    superseded.
    """
    from argosy.state.models import ActionProposal

    del reviewed_symbols
    still_blocking = {
        str(f["symbol"]).upper() for f in decision.material_flagged
    }
    superseded = 0
    try:
        open_rows = (
            db.query(ActionProposal)
            .filter_by(user_id=user_id, kind="deploy_team_flag", status="open")
            .all()
        )
        for row in open_rows:
            sym = (row.dedup_key or "").rsplit(":", 1)[-1].upper()
            if sym not in still_blocking:
                row.status = "superseded"
                superseded += 1
                log.info(
                    "deploy_team.flag_superseded",
                    symbol=sym, proposal_id=row.id,
                    reason="not a current stop-level objection",
                )
        if superseded:
            db.commit()
    except Exception as exc:  # noqa: BLE001 — cleanup is additive/best-effort
        db.rollback()
        log.warning("deploy_team.flag_supersede_failed", error=str(exc)[:160])
    return superseded


__all__ = [
    "build_review_resolution",
    "run_deploy_decision_team",
    "supersede_cleared_flags",
    "write_team_flag_proposals",
    "TeamDecision",
    "DEFAULT_LENSES",
]
