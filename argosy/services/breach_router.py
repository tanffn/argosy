"""T4.5 — auto-route an NVDA concentration-breach SELL tranche to approval (G4).

When NVDA breaches its strategic cap, the monthly cycle creates ONE approval-
pending sell proposal for the next deconcentration tranche: the over-cap amount
(``concentration.nvda_current_pct − cap``) spread evenly over the plan's
optimizer-chosen deconcentration horizon (the persisted doc's glide quarters,
T4.2). It NEVER executes — the proposal lands in ``awaiting_human``, routed into
the SDD §10 approval pipeline, stamped with the plan version (T4.4 lineage).

Idempotent: at most one open deconcentration-tranche proposal at a time (keyed by
a rationale marker), so the monthly cron never spams duplicates while one is
still awaiting the user.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# Rationale marker → idempotency key (no dedicated column needed).
DECON_TRANCHE_MARKER = "deconcentration-tranche"
# Open proposal states that should suppress a new tranche (still in the pipeline).
_OPEN_STATES = ("draft", "cooling", "awaiting_human", "approved")


@dataclass(frozen=True)
class BreachTranche:
    nvda_current_pct: float    # % of the full book held in NVDA today
    nvda_cap_pct: float        # the strategic single-name cap (%)
    over_cap_pct: float        # current − cap (pp); > 0 means breaching
    total_over_cap_nis: float  # the full over-cap sell (NIS)
    n_quarters: int            # the deconcentration horizon in quarters (H×4)
    tranche_nis: float         # one quarter's sell = total_over_cap / n_quarters


def size_deconcentration_tranche(
    *, total_over_cap_nis: float, n_quarters: int
) -> float:
    """One quarter's sell tranche: the over-cap NIS spread evenly across the
    deconcentration horizon. ``n_quarters`` is clamped to ≥ 1."""
    n = max(1, int(n_quarters))
    return max(0.0, float(total_over_cap_nis)) / n


def _doc_glide_quarters(session, user_id: str) -> int | None:
    """Staged-quarter count of the current plan's canonical glide (= H×4, the
    optimizer-chosen horizon baked in by T4.2), or ``None`` when no doc."""
    from argosy.services.target_allocation_doc import load_plan_target_allocation
    from argosy.state.queries import get_current_plan

    try:
        pv = get_current_plan(session, user_id)
        doc = load_plan_target_allocation(pv) if pv is not None else None
    except Exception:  # noqa: BLE001 — defensive
        return None
    if doc is None or not doc.glide:
        return None
    staged = max((w.quarter for w in doc.glide), default=0)
    return staged or None


def compute_breach_tranche(
    session, user_id: str, today: date | None = None
) -> BreachTranche | None:
    """The next NVDA deconcentration tranche when NVDA breaches its cap, else
    ``None`` (not breaching / inputs unavailable). Reuses the SAME over-cap
    resolution the optimizer uses; the book value comes from the lightweight
    plan-numeric resolver (NOT the heavy ``scenario_mc._gather_inputs`` — that
    sets up FX/MC and is far too costly for the monthly tick), and the horizon
    from the persisted doc's glide (already optimizer-chosen, T4.2)."""
    from sqlalchemy import desc, select

    from argosy.services.plan_numeric_resolver import resolve_plan_numbers
    from argosy.services.retirement.deconcentration_optimizer import _resolve_nvda_sell
    from argosy.state.models import PlanVersion

    try:
        pv = session.execute(
            select(PlanVersion)
            .where(PlanVersion.user_id == user_id, PlanVersion.role == "current")
            .order_by(desc(PlanVersion.id))
            .limit(1)
        ).scalar_one_or_none()
        drun = getattr(pv, "decision_run_id", None) if pv else None
        if drun is None:
            return None
        nums = resolve_plan_numbers(session, user_id=user_id, decision_run_id=int(drun))
        nw = nums.get("portfolio.net_worth_nis")
        book = (
            float(nw.value)
            if (nw is not None and getattr(nw, "status", None) == "resolved" and nw.value)
            else 0.0
        )
    except Exception:  # noqa: BLE001 — defensive; no book ⇒ no tranche
        return None
    if book <= 0:
        return None
    sell_nis, nvda_frac, cap_frac = _resolve_nvda_sell(session, user_id, book)
    if nvda_frac is None or cap_frac is None or sell_nis <= 0:
        return None  # at/under cap, or unavailable

    n_q = _doc_glide_quarters(session, user_id) or 8
    return BreachTranche(
        nvda_current_pct=round(nvda_frac * 100.0, 2),
        nvda_cap_pct=round(cap_frac * 100.0, 2),
        over_cap_pct=round((nvda_frac - cap_frac) * 100.0, 2),
        total_over_cap_nis=round(sell_nis, 2),
        n_quarters=int(n_q),
        tranche_nis=round(size_deconcentration_tranche(
            total_over_cap_nis=sell_nis, n_quarters=n_q), 2),
    )


def route_breach_tranche(
    session, user_id: str, today: date | None = None
) -> int | None:
    """Create ONE ``awaiting_human`` NVDA sell-tranche proposal when NVDA
    breaches its cap and none is already open. Returns the new proposal id, or
    ``None`` (no breach / already an open tranche). NEVER executes — routes to
    approval. Best-effort: the caller wraps this so the cycle never breaks."""
    from sqlalchemy import select

    from argosy.state.models import Proposal
    from argosy.state.queries import get_current_plan

    tranche = compute_breach_tranche(session, user_id, today)
    if tranche is None:
        return None

    # Idempotency: suppress when a deconcentration tranche is already in flight.
    existing = session.execute(
        select(Proposal.id).where(
            Proposal.user_id == user_id,
            Proposal.ticker == "NVDA",
            Proposal.action == "sell",
            Proposal.status.in_(_OPEN_STATES),
            Proposal.rationale_summary.like(f"%{DECON_TRANCHE_MARKER}%"),
        ).limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return None

    pv = get_current_plan(session, user_id)
    plan_version_id = getattr(pv, "id", None)

    row = Proposal(
        user_id=user_id,
        ticker="NVDA",
        action="sell",
        size_shares_or_currency=tranche.tranche_nis,
        size_units="currency",
        instrument="stock",
        order_type="market",
        tier="T2",
        status="awaiting_human",
        rationale_summary=(
            f"[{DECON_TRANCHE_MARKER}] NVDA {tranche.nvda_current_pct:.1f}% > "
            f"{tranche.nvda_cap_pct:.1f}% cap; sell tranche of the over-cap "
            f"position spread over {tranche.n_quarters} quarters. Awaiting approval."
        ),
        plan_version_id=plan_version_id,
    )
    session.add(row)
    session.flush()
    return int(row.id)
