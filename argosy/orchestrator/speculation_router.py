"""Routes accepted speculative candidates from `current.short` -> Argonaut.

Wave 3. Reads the user's `role='current'` plan, finds the requested
speculative candidate by ticker, applies the speculation cap one more
time (defense-in-depth), and creates a T0 proposal targeting the
Argonaut account.

In `paper` mode, the proposal lands as `paper=True` and is recorded as a
PaperFill via the existing decision_flow infrastructure (SDD §9.2).

In `live` mode, the SDD §10.1 routing matrix applies: T0 + Argonaut +
live = auto-execute. The router defers that policy to the existing
proposal lifecycle.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from argosy.config import get_user_agent_settings, load_speculation_cap
from argosy.logging import get_logger
from argosy.state.queries import get_current_plan

log = get_logger(__name__)


class UnknownCandidateError(Exception):
    """No speculative candidate with the given ticker in current.short."""


class CapBreachError(Exception):
    """The candidate exceeds the user's speculation cap (defense-in-depth)."""


@dataclass
class RouteResult:
    proposal_id: int
    ticker: str
    paper: bool


def route_accepted_candidate(
    session: Session,
    *,
    user_id: str,
    ticker: str,
    execution_mode: Literal["paper", "live", "queue_only"],
) -> RouteResult:
    """Create a T0 Argonaut proposal for the named candidate."""
    pv = get_current_plan(session, user_id)
    if pv is None or not pv.horizon_short_json:
        raise UnknownCandidateError(f"no current plan or short horizon for {user_id}")

    short = json.loads(pv.horizon_short_json)
    candidate = next(
        (c for c in (short.get("speculative_candidates") or [])
         if c.get("ticker", "").upper() == ticker.upper()),
        None,
    )
    if candidate is None:
        raise UnknownCandidateError(
            f"no speculative candidate for ticker {ticker!r} in current.short"
        )

    cap = load_speculation_cap(
        user_id=user_id, agent_settings=get_user_agent_settings(user_id),
    )
    pct = float(candidate.get("suggested_position_pct_of_net_worth", 0))
    if pct > cap.max_pct_of_net_worth:
        raise CapBreachError(
            f"candidate {ticker} pct={pct} exceeds cap={cap.max_pct_of_net_worth}"
        )
    if not candidate.get("risk_ceiling_check"):
        raise CapBreachError(
            f"candidate {ticker} risk_ceiling_check is false"
        )

    # Wave 3 spec-compliance fix: honour the user's
    # ``cap.allowed_account_classes``.  An empty tuple means "speculation
    # disabled" — the router refuses to route.  Otherwise we route to the
    # first allowed class (the spec frames this as a configured class, not
    # a per-candidate selection — there is no per-candidate account-class
    # field).
    if not cap.allowed_account_classes:
        raise CapBreachError(
            "speculation disabled — allowed_account_classes is empty"
        )
    target_account_class = cap.allowed_account_classes[0]

    paper = execution_mode != "live"
    # T4.2: forward the candidate's ``sourced_from`` list onto the
    # persisted proposal so the /api/proposals route can surface the
    # citations under ``cited_sources`` (see proposal_lifecycle for the
    # storage shape: it lands inside ``expected_impact_json``).
    sourced_from_raw = candidate.get("sourced_from") or []
    sourced_from = (
        [str(x) for x in sourced_from_raw if isinstance(x, (str, int, float))]
        if isinstance(sourced_from_raw, list)
        else []
    )
    proposal = _create_proposal(
        session=session,
        user_id=user_id,
        ticker=ticker.upper(),
        action="buy",
        size_usd=float(candidate["suggested_position_usd"]),
        order_type="limit",
        tier="T0",
        account_class=target_account_class,
        rationale_summary=candidate.get("thesis_summary", ""),
        exit_trigger=candidate.get("exit_trigger", ""),
        execution_mode=execution_mode,
        paper=paper,
        # I1 audit lineage: thread the originating plan's decision_run_id
        # through so the routed proposal links back to the synthesis run
        # that emitted the candidate (per SDD §6.11).
        decision_run_id=getattr(pv, "decision_run_id", None),
        sourced_from=sourced_from,
    )
    session.commit()

    # C2 defense-in-depth: the helper sets ``account_class`` from our
    # ``target_account_class``, but a future regression in the helper
    # could override it (e.g. a default arg flip).  Re-check that the
    # persisted row's class is still in the allowed set.
    if proposal.account_class not in cap.allowed_account_classes:
        raise CapBreachError(
            f"routed proposal {proposal.id} has account_class="
            f"{proposal.account_class!r} which is not in cap "
            f"allowed_account_classes={cap.allowed_account_classes!r}"
        )

    # M7: emit a WS event so subscribed UIs (notably the Argonaut tab)
    # surface the routed proposal without waiting for the next refresh.
    # Best-effort fire-and-forget — failure to publish must never break
    # the route.
    try:
        from argosy.api.events import publish_event_threadsafe

        publish_event_threadsafe(
            "plan.speculative.routed",
            {
                "user_id": user_id,
                "ticker": ticker.upper(),
                "proposal_id": proposal.id,
                "paper": paper,
            },
        )
    except Exception:  # noqa: BLE001 — defensive; events are best-effort
        log.warning("speculation_router.publish_failed", proposal_id=proposal.id)

    log.info(
        "speculation_router.routed",
        user_id=user_id,
        ticker=ticker,
        proposal_id=proposal.id,
        paper=paper,
    )
    return RouteResult(proposal_id=proposal.id, ticker=ticker.upper(), paper=paper)


def _create_proposal(**kw):
    """Indirection point so tests can monkeypatch.

    Wave 3: delegates to ``argosy.orchestrator.proposal_lifecycle``,
    which is the synchronous helper that writes ``proposals`` rows from
    a synthesized speculative candidate. That helper exists alongside
    the async ``DecisionFlow._persist_proposal`` path used by the full
    analyst -> trader -> fund-manager pipeline; the speculation router
    short-circuits past that pipeline because the candidate already
    arrived from the synthesizer with a thesis, size, and exit trigger
    pre-attached.
    """
    from argosy.orchestrator.proposal_lifecycle import create_speculative_proposal
    return create_speculative_proposal(**kw)


__all__ = [
    "CapBreachError",
    "RouteResult",
    "UnknownCandidateError",
    "route_accepted_candidate",
]
