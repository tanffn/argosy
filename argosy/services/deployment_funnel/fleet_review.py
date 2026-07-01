"""Increment 2 — LIVE fleet adjudication of NEEDS_FLEET_REVIEW candidates.

The deterministic layer (``gates.py``) reconciles a deploy against the plan's own
numbers and REFUSES to invent an investment judgment. When a buy raises a genuine
judgment the plan number can't answer — e.g. adding NVDA-correlated exposure while
the book is already at/over the plan's concentration cap, or a low-conviction
discovery pick — the candidate is marked ``NEEDS_FLEET_REVIEW`` and its dollars are
HELD. This module ROUTES those held candidates to the agent fleet:

  * RiskOfficer (3 perspectives: aggressive / neutral / conservative) — the
    concentration / sizing / pacing risk judgment.
  * FundManager — the final integrity + selection/overlap green-light.

Their verdicts are mapped to a BOUNDED action (APPROVE / CAP_AT_PCT / VETO /
MOVE_TO_RESERVE) — the agents adjudicate the GIVEN candidate; they never invent a
new instrument or a size outside the plan. The mapping is deterministic given the
verdicts, so it is fully unit-testable with canned agent outputs (inject via the
``*_factory`` params, mirroring ``argosy/decisions/flow.py``).

Fail-open by contract: any agent error leaves the candidate as NEEDS_FLEET_REVIEW
(held + surfaced) — nothing is silently approved. The route only calls this when
``deployment_fleet_review_enabled`` AND ``live=True``.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, replace
from typing import Awaitable, Callable

from argosy.agents.fund_manager import FundManagerAgent
from argosy.agents.risk_officer import Perspective, RiskOfficerAgent
from argosy.services.deployment_funnel.contracts import (
    CandidateStatus,
    EnrichedCandidate,
)

_PERSPECTIVES: tuple[Perspective, ...] = ("aggressive", "neutral", "conservative")

# Factory types — a test injects canned agents; production uses the real classes.
RiskOfficerFactory = Callable[[str, Perspective], RiskOfficerAgent]
FundManagerFactory = Callable[[str], FundManagerAgent]


@dataclass(frozen=True)
class DeploymentContext:
    """Compact, plan-bound context the fleet adjudicates against. Built by the
    route from the plan + holdings + (optional) live market context."""

    book_usd: float
    current_effective_nvda_usd: float
    nvda_cap_pct: float
    plan_classes: tuple[str, ...]
    user_constraints: str = ""
    market_note: str = ""

    @property
    def book_nvda_pct(self) -> float:
        return (
            100.0 * self.current_effective_nvda_usd / self.book_usd
            if self.book_usd > 0
            else 100.0
        )


@dataclass(frozen=True)
class FleetAdjudication:
    """The fleet's bounded verdict for one held candidate."""

    symbol: str
    action: CandidateStatus
    cap_pct: float | None
    rationale: str
    cited_sources: tuple[str, ...] = ()


def _proposal_for(cand: EnrichedCandidate, ctx: DeploymentContext) -> dict:
    """A deployment-decision proposal dict the agents render + adjudicate. NOT a
    tactical trade — it states the plan-bound facts so the fleet judges the ONE
    open question the engine wouldn't decide."""
    notional = cand.candidate.total_notional_usd
    inst_wt_pct = (
        100.0 * cand.effective_nvda_usd / notional if notional > 0 else 0.0
    )
    return {
        "decision": "deploy_cash_candidate",
        "symbol": cand.symbol,
        "buy_notional_usd": round(notional, 2),
        "instrument_nvda_lookthrough_pct": round(inst_wt_pct, 1),
        "book_nvda_lookthrough_pct": round(ctx.book_nvda_pct, 1),
        "nvda_plan_cap_pct": ctx.nvda_cap_pct,
        "why_routed": cand.reason,
        # The deterministic FACTS the engine surfaced — the fleet judges THESE.
        "flags": [
            {"kind": f.kind, "materiality": f.materiality, "fact": f.fact}
            for f in getattr(cand, "flags", ())
        ],
        "news_sentiment": cand.news_sentiment or "none",
        "plan_sleeves": list(ctx.plan_classes),
        "market_note": ctx.market_note or "n/a",
        "question": (
            "The book is at/over the plan's NVDA concentration cap. Should this "
            "cash-funded buy proceed as-is, be size-capped, be vetoed (deconcentrate "
            "instead / route cash to zero-NVDA diversifiers), or be parked in the "
            "reserve? Judge against the prime directive (earliest safe retirement), "
            "not risk-avoidance alone."
        ),
    }


_CUT_RE = re.compile(r"(?:cut|reduce|trim|size)[^0-9]{0,20}(\d{1,3}(?:\.\d+)?)\s*%")
_TO_RE = re.compile(r"(?:to|at)\s+(\d{1,3}(?:\.\d+)?)\s*%")


def _size_from_conditions(conditions: list[str], default_keep_pct: float = 50.0) -> float:
    """Parse a KEEP-% from risk-officer conditions text. 'cut size 40%' -> keep 60;
    'reduce to 25%' -> keep 25. Falls back to a conservative 50% keep when the
    condition names no number. Clamped to (0, 100]."""
    parsed: list[float] = []
    for c in conditions:
        cl = c.lower()
        m_to = _TO_RE.search(cl)
        m_cut = _CUT_RE.search(cl)
        if m_to:
            parsed.append(float(m_to.group(1)))
        elif m_cut:
            parsed.append(100.0 - float(m_cut.group(1)))
    keep = min(parsed) if parsed else default_keep_pct  # strictest keep wins
    return max(1.0, min(100.0, keep))


def _map_verdicts_to_action(
    *,
    symbol: str,
    risk_verdicts: list,
    fm_decision,
) -> FleetAdjudication:
    """Bounded, deterministic mapping from agent verdicts to a CandidateStatus.

    - FundManager BLOCK -> VETO (the integrity gate said no).
    - else majority of the 3 risk officers REJECT -> VETO (don't add the exposure).
    - else any APPROVE_WITH_CONDITIONS -> CAP_AT_PCT to the strictest keep-% the
      conditions imply (default keep 50%).
    - else (FM green + risk consensus APPROVE) -> APPROVE.
    """
    cites: list[str] = list(getattr(fm_decision, "cited_sources", []) or [])
    for v in risk_verdicts:
        cites.extend(getattr(v, "cited_sources", []) or [])
    cites_t = tuple(dict.fromkeys(cites))  # de-dup, preserve order

    verdict_of = [getattr(v, "verdict", "REJECT") for v in risk_verdicts]
    n_reject = verdict_of.count("REJECT")
    n_cond = verdict_of.count("APPROVE_WITH_CONDITIONS")

    fm = getattr(fm_decision, "decision", "block")
    if fm == "block":
        return FleetAdjudication(
            symbol, CandidateStatus.VETO, None,
            f"fund manager BLOCK: {getattr(fm_decision, 'reason', '')}", cites_t,
        )

    if n_reject * 2 >= len(risk_verdicts) and risk_verdicts:
        return FleetAdjudication(
            symbol, CandidateStatus.VETO, None,
            f"risk officers rejected ({n_reject}/{len(risk_verdicts)}) — "
            "deconcentrate / route cash to diversifiers instead", cites_t,
        )

    if n_cond > 0:
        conditions: list[str] = []
        for v in risk_verdicts:
            conditions.extend(getattr(v, "conditions", []) or [])
        keep = _size_from_conditions(conditions)
        return FleetAdjudication(
            symbol, CandidateStatus.CAP_AT_PCT, round(keep, 1),
            f"fleet approved with conditions — cap to {keep:.0f}% of the buy "
            f"({n_cond}/{len(risk_verdicts)} risk officers conditioned)", cites_t,
        )

    return FleetAdjudication(
        symbol, CandidateStatus.APPROVE, None,
        "fleet green-light: risk consensus APPROVE + fund-manager green light",
        cites_t,
    )


async def _adjudicate_one(
    cand: EnrichedCandidate,
    ctx: DeploymentContext,
    *,
    user_id: str,
    risk_officer_factory: RiskOfficerFactory,
    fund_manager_factory: FundManagerFactory,
) -> FleetAdjudication:
    proposal = _proposal_for(cand, ctx)
    risk_caps = {"nvda_concentration_cap_pct": ctx.nvda_cap_pct}
    analyst_reports: list[dict] = [
        {"agent_role": "deployment_engine",
         "held_reason": cand.reason,
         "book_nvda_pct": round(ctx.book_nvda_pct, 1)}
    ]

    async def _run_risk(p: Perspective):
        agent = risk_officer_factory(user_id, p)
        report = await agent.run(
            proposal=proposal, analyst_reports=analyst_reports,
            user_constraints=ctx.user_constraints, risk_caps=risk_caps,
            round_index=1, n_max=1,
        )
        return report.output

    # Tolerate a flaky agent backend: gather with return_exceptions so ONE failing
    # risk officer (e.g. a transient claude.exe exit-1) doesn't sink the whole
    # candidate. Require a majority (>=2 of 3) to have responded for a real
    # consensus; otherwise raise -> _guarded holds it (fail-closed, never a
    # verdict off a single voice).
    raw = await asyncio.gather(
        *[_run_risk(p) for p in _PERSPECTIVES], return_exceptions=True
    )
    risk_verdicts = [r for r in raw if not isinstance(r, BaseException)]
    if len(risk_verdicts) < 2:
        raise RuntimeError(
            f"insufficient risk-officer responses "
            f"({len(risk_verdicts)}/{len(_PERSPECTIVES)}) — holding"
        )

    consensus = {
        "verdicts": [
            {"perspective": getattr(v, "perspective", "?"),
             "verdict": getattr(v, "verdict", "?")}
            for v in risk_verdicts
        ]
    }
    fm_agent = fund_manager_factory(user_id)
    fm_report = await fm_agent.run(
        decision_kind="trade_proposal", proposal=proposal,
        risk_outcome=consensus, plan_critique=None,
        user_constraints=ctx.user_constraints, tier="core",
    )
    return _map_verdicts_to_action(
        symbol=cand.symbol, risk_verdicts=risk_verdicts,
        fm_decision=fm_report.output,
    )


async def adjudicate_candidates(
    enriched: list[EnrichedCandidate] | tuple[EnrichedCandidate, ...],
    *,
    context: DeploymentContext,
    user_id: str,
    risk_officer_factory: RiskOfficerFactory | None = None,
    fund_manager_factory: FundManagerFactory | None = None,
) -> dict[str, FleetAdjudication]:
    """Adjudicate every NEEDS_FLEET_REVIEW candidate. Returns {symbol: verdict}.
    A candidate whose adjudication raises is OMITTED from the result (the caller
    leaves it NEEDS_FLEET_REVIEW — fail-open, held + surfaced, never auto-approved).
    Non-review candidates are ignored."""
    ro_factory = risk_officer_factory or (
        lambda u, p: RiskOfficerAgent(user_id=u, perspective=p)
    )
    fm_factory = fund_manager_factory or (lambda u: FundManagerAgent(user_id=u))

    targets = [
        e for e in enriched if e.status is CandidateStatus.NEEDS_FLEET_REVIEW
    ]

    async def _guarded(e: EnrichedCandidate):
        try:
            return await _adjudicate_one(
                e, context, user_id=user_id,
                risk_officer_factory=ro_factory,
                fund_manager_factory=fm_factory,
            )
        except Exception:  # noqa: BLE001 — fail-open: leave the candidate held
            return None

    results = await asyncio.gather(*[_guarded(e) for e in targets])
    return {r.symbol: r for r in results if r is not None}


def apply_adjudications(
    enriched: tuple[EnrichedCandidate, ...] | list[EnrichedCandidate],
    adjudications: dict[str, FleetAdjudication],
) -> list[EnrichedCandidate]:
    """Return a new enriched list with each adjudicated candidate's status/reason/
    cap_pct replaced by the fleet's bounded verdict. Un-adjudicated candidates are
    unchanged (still NEEDS_FLEET_REVIEW when the fleet errored)."""
    out: list[EnrichedCandidate] = []
    for e in enriched:
        adj = adjudications.get(e.symbol)
        if adj is not None and e.status is CandidateStatus.NEEDS_FLEET_REVIEW:
            out.append(replace(
                e, status=adj.action, cap_pct=adj.cap_pct,
                reason=f"[fleet] {adj.rationale}",
            ))
        else:
            out.append(e)
    return out


def adjudicate_sync(
    enriched: tuple[EnrichedCandidate, ...] | list[EnrichedCandidate],
    *,
    context: DeploymentContext,
    user_id: str,
    risk_officer_factory: RiskOfficerFactory | None = None,
    fund_manager_factory: FundManagerFactory | None = None,
) -> list[EnrichedCandidate]:
    """Sync bridge for the (sync) /deploy-cash route: adjudicate + apply. Safe to
    call from a FastAPI sync route (runs in a worker thread, no running loop)."""
    adjudications = asyncio.run(
        adjudicate_candidates(
            enriched, context=context, user_id=user_id,
            risk_officer_factory=risk_officer_factory,
            fund_manager_factory=fund_manager_factory,
        )
    )
    return apply_adjudications(enriched, adjudications)


async def recommend_disposition(
    enriched: tuple[EnrichedCandidate, ...] | list[EnrichedCandidate],
    *,
    context: DeploymentContext,
    deployable_usd: float,
    user_id: str,
    agent_factory: Callable[[str], object] | None = None,
):
    """The fleet answers "what should I DO with this cash?" — an affirmative
    disposition of the FULL amount (deploy / hold-with-reason / deconcentrate /
    plan-change), so cash is never silent residue. Returns a DeploymentDisposition
    or None on agent failure (caller surfaces the deterministic state instead)."""
    from argosy.agents.deployment_disposition import DeploymentDispositionAgent

    approved = [
        {"symbol": e.symbol, "usd": round(e.candidate.total_notional_usd, 2)}
        for e in enriched if e.status is CandidateStatus.APPROVE
    ]
    blocked = [
        {"symbol": e.symbol,
         "usd": round(e.candidate.total_notional_usd, 2),
         "facts": [f.fact for f in getattr(e, "flags", ())] or [e.reason]}
        for e in enriched
        if e.status in (CandidateStatus.NEEDS_FLEET_REVIEW, CandidateStatus.VETO,
                        CandidateStatus.CAP_AT_PCT)
    ]
    agent = (agent_factory or (lambda u: DeploymentDispositionAgent(user_id=u)))(user_id)
    try:
        report = await agent.run(
            deployable_usd=deployable_usd,
            book_nvda_pct=context.book_nvda_pct,
            nvda_cap_pct=context.nvda_cap_pct,
            plan_sleeves=list(context.plan_classes),
            already_deploying=approved,
            blocked=blocked,
            reserve_funded=True,
            user_constraints=context.user_constraints,
        )
        return report.output
    except Exception:  # noqa: BLE001 — caller falls back to the deterministic view
        return None


def recommend_disposition_sync(
    enriched,
    *,
    context: DeploymentContext,
    deployable_usd: float,
    user_id: str,
    agent_factory: Callable[[str], object] | None = None,
):
    """Sync bridge for the (sync) route."""
    return asyncio.run(
        recommend_disposition(
            enriched, context=context, deployable_usd=deployable_usd,
            user_id=user_id, agent_factory=agent_factory,
        )
    )


__all__ = [
    "DeploymentContext",
    "FleetAdjudication",
    "adjudicate_candidates",
    "apply_adjudications",
    "adjudicate_sync",
    "recommend_disposition",
    "recommend_disposition_sync",
]
