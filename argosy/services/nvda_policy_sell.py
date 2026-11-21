"""The NVDA sell assessor — the sell half of the period directive.

The plan's glide is the BASE POLICY: deconcentrate NVDA toward its cap over the
optimizer-chosen horizon. On top of that sits the EXCEPTION PROTOCOL — sells that
fire outside the routine glide cadence, categorised by *why*:

  * ``policy``       — the scheduled glide tranche (this quarter's paced sale).
  * ``thesis-break`` — the reason for holding materially changed; accelerate the
                       trim to the cap NOW rather than pacing it over quarters.
  * (``risk-budget`` / ``catch-up`` land in later increments.)

Selection rule (codex methodology): convert each active category to a post-trade
TARGET WEIGHT, take the *lowest* target, and size ONE sale to it — the largest
justified sale satisfies every smaller one, so the tax and the over-cap amount are
never double-counted. Precedence when equal: thesis-break > risk-budget > catch-up
> policy.

Verdicts:
  * ``sell_due``  — a sale is due; ``tranche_nis`` is the recommended sale.
  * ``no_action`` — NVDA within its cap; nothing to sell (an explicit stance).
  * ``unavailable`` — the available evidence cannot establish a sell or a hold.

Read-only: it NEVER writes a proposal or executes. Reuses the codex-verified
breach-tranche money-math and the existing thesis-flag loader.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from math import isfinite

from argosy.logging import get_logger
from argosy.services.breach_router import compute_breach_tranche
from argosy.services.nvda_catchup import catchup_sale_nis
from argosy.services.nvda_risk_budget import risk_budget_sale_nis

# Approved idiosyncratic NVDA shock for the risk-budget floor test (codex: 40–50%).
_RISK_SHOCK = 0.40

_log = get_logger("argosy.nvda_policy_sell")

# A sell realizes Israeli CGT on §102 equity lots; the glide's whole point is to
# pace the deconcentration so the tax is spread rather than taken in one hit.
_TAX_NOTE = (
    "Realizes Israeli CGT on §102 equity lots — paced over the glide so the tax "
    "is spread, not taken in one hit."
)
_TAX_NOTE_ACCEL = (
    "Realizes Israeli CGT on §102 equity lots in one larger sale — the thesis "
    "break outweighs spreading the tax over the glide."
)
_RISK_TAX_NOTE = "The risk estimate is before tax. Assess current trustee lots and net proceeds before sizing a trade."


@dataclass(frozen=True)
class NvdaPolicySell:
    status: str            # "sell_due" | "no_action" | "unavailable"
    category: str          # "policy" | "thesis-break" | (risk-budget / catch-up later)
    tranche_nis: float     # the RECOMMENDED sale this period (not always a /n tranche)
    nvda_current_pct: float | None
    nvda_cap_pct: float | None
    n_quarters: int
    headline: str
    tax_note: str
    notes: tuple[str, ...] = field(default_factory=tuple)


def _funnel_concurs_reduce(session, user_id: str, ticker: str = "NVDA") -> bool:
    """True when the daily decision funnel currently has an open reduce (sell/trim)
    proposal for ``ticker`` — the lightweight bridge that lets the unified sell note
    the funnel's concurrence. Best-effort: False on any error."""
    try:
        from sqlalchemy import select

        from argosy.state.models import Proposal

        row = session.execute(
            select(Proposal.id).where(
                Proposal.user_id == user_id,
                Proposal.ticker == ticker,
                Proposal.source == "decision_funnel",
                Proposal.action.in_(("sell", "trim")),
                Proposal.status.in_(("draft", "cooling", "awaiting_human", "approved")),
            ).limit(1)
        ).first()
        return row is not None
    except Exception:  # noqa: BLE001 — advisory bridge; absence on error
        return False


def _resolve_catchup_inputs(session, user_id: str, today: date, tranche_nis: float) -> tuple[int, int]:
    """(waypoints_due, tranches_executed) for the catch-up check:
      * waypoints_due    — glide quarters (>= q1) whose date has passed as of today.
      * tranches_executed — SCHEDULED deconcentration executed, in tranche-equivalents.

    Only the plan's scheduled deconcentration sells count (filtered by the breach
    router's ``DECON_TRANCHE_MARKER``) — an ordinary/funnel/risk/thesis NVDA sale is
    NOT a scheduled tranche and must not suppress catch-up (codex). And it is measured
    by executed AMOUNT ÷ tranche size, not proposal count, so one sale sized at several
    tranches credits all of them. Best-effort → (0, 0) → no catch-up on error."""
    try:
        from sqlalchemy import func, select

        from argosy.services.breach_router import DECON_TRANCHE_MARKER
        from argosy.services.target_allocation_doc import load_plan_target_allocation
        from argosy.state.models import Proposal
        from argosy.state.queries import get_current_plan

        pv = get_current_plan(session, user_id)
        doc = load_plan_target_allocation(pv) if pv is not None else None
        if doc is None or not getattr(doc, "glide", None) or tranche_nis <= 0:
            return 0, 0
        waypoints_due = sum(
            1 for w in doc.glide
            if getattr(w, "quarter", 0) >= 1 and getattr(w, "date", None) is not None
            and w.date <= today
        )
        executed_nis = session.execute(
            select(func.coalesce(func.sum(Proposal.size_shares_or_currency), 0.0)).where(
                Proposal.user_id == user_id,
                Proposal.ticker == "NVDA",
                Proposal.action == "sell",
                Proposal.status.like("executed%"),
                Proposal.rationale_summary.like(f"%{DECON_TRANCHE_MARKER}%"),
            )
        ).scalar_one()
        # Floor, but with a tiny epsilon so an EXACT multiple executed as one
        # multi-tranche sale (e.g. 999999.99 / 333333.33) isn't undercounted by
        # float imprecision — while a genuine partial tranche still floors down.
        tranches_executed = int(float(executed_nis or 0.0) / float(tranche_nis) + 1e-6)
        return int(waypoints_due), tranches_executed
    except Exception:  # noqa: BLE001 — best-effort; no catch-up on error
        _log.warning("nvda_sell.catchup_inputs_failed", user_id=user_id)
        return 0, 0


@dataclass(frozen=True)
class _PolicyInputs:
    weight_pct: float | None = None
    cap_pct: float | None = None
    risk_budget: tuple[float, float, float] | None = None
    missing_risk_inputs: tuple[str, ...] = ()


def _resolve_policy_inputs(session, user_id: str) -> _PolicyInputs:
    """Resolve measured concentration and the liquid capital, position and perpetuity floor."""
    try:
        from sqlalchemy import desc, select

        from argosy.services.holding_books import (
            implied_nvda_weight_frac,
            tradeable_securities_nis_for_user,
        )
        from argosy.services.plan_numeric_resolver import resolve_plan_numbers
        from argosy.state.models import PlanVersion

        pv = session.execute(
            select(PlanVersion)
            .where(PlanVersion.user_id == user_id, PlanVersion.role == "current")
            .order_by(desc(PlanVersion.id))
            .limit(1)
        ).scalar_one_or_none()
        drun = getattr(pv, "decision_run_id", None) if pv else None
        if drun is None:
            return _PolicyInputs()
        nums = resolve_plan_numbers(session, user_id=user_id, decision_run_id=int(drun))

        def _v(key):
            n = nums.get(key)
            if n is None or getattr(n, "status", None) != "resolved" or n.value is None:
                return None
            value = float(n.value)
            return value if isfinite(value) and value >= 0 else None

        nw = _v("portfolio.liquid_net_worth_nis")
        nvda = _v("concentration.nvda_value_nis")
        floor = _v("retirement.fi_target_nis")
        weight_record = nums.get("concentration.nvda_current_pct")
        tradeable_nis = (
            tradeable_securities_nis_for_user(session, user_id)
            if getattr(weight_record, "status", None) == "excluded" else None
        )
        weight = implied_nvda_weight_frac(nums, tradeable_securities_nis=tradeable_nis)
        cap = _v("concentration.nvda_cap_pct")
        return _PolicyInputs(
            weight_pct=weight * 100 if weight is not None else None,
            cap_pct=cap * 100 if cap is not None else None,
            risk_budget=(nw, nvda, floor) if nw and nvda is not None and floor else None,
            missing_risk_inputs=tuple(
                label for label, missing in (
                    ("liquid capital", not nw), ("NVDA position value", nvda is None),
                    ("retirement floor", not floor),
                ) if missing
            ),
        )
    except Exception:  # noqa: BLE001 — assessment remains explicitly incomplete
        _log.warning("nvda_sell.policy_inputs_failed", user_id=user_id)
        return _PolicyInputs()


def _load_nvda_thesis_flags(session, user_id: str, now: datetime) -> list[dict] | None:
    """Active thesis-monitor flags for NVDA (weakened / broken), via the same
    loader ``holistic_rebalance_review`` uses.

    Returns a (possibly empty) list on success, or ``None`` when the read FAILS.
    The caller must distinguish these: ``[]`` means "verified, no flags" while
    ``None`` means "unknown" — an unknown state must not masquerade as clear (a
    real break must not be silently downgraded to the policy pace unnoticed)."""
    try:
        from argosy.services.holistic_rebalance_review import _load_active_thesis_flags

        flags = _load_active_thesis_flags(
            session, user_id, now=now, known_tickers={"NVDA"}
        )
        return [f for f in flags if (f.get("ticker") or "").upper() == "NVDA"]
    except Exception:  # noqa: BLE001 — signal read failed; caller flags it unverified
        _log.warning("nvda_sell.thesis_flag_load_failed", user_id=user_id)
        return None


def assess_nvda_policy_sell(
    *, session, user_id: str, today: date | None = None
) -> NvdaPolicySell:
    """Read-only NVDA sell verdict for ``user_id`` as of ``today``."""
    tranche = compute_breach_tranche(session, user_id, today)

    # Risk-budget is INDEPENDENT of the concentration cap: an under-cap position can
    # still be large enough that a plausible NVDA drawdown breaches the FI floor.
    # Size it first so it can fire even when nothing is over-cap.
    risk_sale = 0.0
    inputs = _resolve_policy_inputs(session, user_id)
    rb = inputs.risk_budget
    # The arithmetic caps the recommendation at the position. Report when
    # even that sale cannot restore a floor already above current capital.
    floor_attainable = rb is not None and rb[0] >= rb[2]
    if rb is not None:
        nw, nvda_val, floor = rb
        risk_sale = risk_budget_sale_nis(
            net_worth_nis=nw, nvda_value_nis=nvda_val, perpetuity_base_nis=floor,
            shock=_RISK_SHOCK,
        )

    # Bridge: does the daily decision funnel also flag NVDA to reduce? (A note that
    # strengthens the case in the unified sell, not a competing size.)
    _funnel_note = (
        "The daily decision funnel (beta) also flagged NVDA to reduce — the fleet "
        "concurs with trimming.",
    ) if _funnel_concurs_reduce(session, user_id) else ()

    if tranche is None:
        if risk_sale <= 0:
            within_cap = (
                inputs.weight_pct is not None and inputs.cap_pct is not None
                and inputs.weight_pct <= inputs.cap_pct
            )
            if not within_cap or not floor_attainable:
                reasons = []
                if inputs.weight_pct is None:
                    reasons.append("Current NVDA weight is unavailable.")
                if inputs.cap_pct is None:
                    reasons.append("The concentration cap is unavailable.")
                if inputs.weight_pct is not None and inputs.cap_pct is not None and not within_cap:
                    reasons.append("NVDA exceeds its cap and a glide tranche could not be sized.")
                if rb is None:
                    if inputs.missing_risk_inputs:
                        reasons.append("The retirement stress test needs " + " and ".join(inputs.missing_risk_inputs) + ".")
                    else:
                        reasons.append("The retirement stress test inputs are unavailable.")
                elif not floor_attainable:
                    reasons.append("Liquid capital is already below the retirement floor. An NVDA sale alone cannot restore it.")
                return NvdaPolicySell(
                    status="unavailable", category="policy", tranche_nis=0.0,
                    nvda_current_pct=inputs.weight_pct, nvda_cap_pct=inputs.cap_pct, n_quarters=0,
                    headline="NVDA sell assessment incomplete. " + " ".join(reasons), tax_note="",
                )
            return NvdaPolicySell(
                status="no_action", category="policy", tranche_nis=0.0,
                nvda_current_pct=inputs.weight_pct, nvda_cap_pct=inputs.cap_pct, n_quarters=0,
                headline=(
                    "NVDA is within its concentration cap and a plausible drawdown "
                    "leaves your retirement floor intact — no sell is due this period."
                ),
                tax_note="",
            )
        risk_headline = (
            f"A {_RISK_SHOCK*100:.0f}% NVDA drawdown would breach your retirement floor. "
            f"The risk-budget calculation calls for a gross sale of ~₪{risk_sale:,.0f}."
        ) if floor_attainable else (
            f"Liquid capital is already below the retirement floor. The risk-budget calculation "
            f"reaches the full NVDA position (~₪{risk_sale:,.0f}). Even that gross sale cannot restore the floor."
        )
        return NvdaPolicySell(
            status="sell_due", category="risk-budget", tranche_nis=round(risk_sale, 2),
            nvda_current_pct=inputs.weight_pct, nvda_cap_pct=inputs.cap_pct, n_quarters=1,
            headline=risk_headline,
            tax_note=_RISK_TAX_NOTE,
            notes=_funnel_note,
        )

    now = datetime.now(timezone.utc)
    flags = _load_nvda_thesis_flags(session, user_id, now)
    # Unknown thesis state (read failed) must NOT look like "clear": hold the
    # policy pace (never over-sell on no signal) but flag it unverified so a real
    # break is not silently downgraded unnoticed. thesis-break > policy is only
    # safe to *skip* when we actually verified there is no break.
    thesis_unverified = flags is None
    flags = flags or []
    # Confluence guard: only a BROKEN thesis the monitor escalated to CRITICAL
    # acts. A weakened/warning flag surfaces a review but does not resize the trim
    # (no acting on a soft single signal).
    broken = any(
        f.get("kind") == "thesis_monitor_broken" and f.get("severity") == "critical"
        for f in flags
    )
    weakened = (not broken) and any(
        f.get("kind", "").startswith("thesis_monitor_") for f in flags
    )

    # Convert each active category to its recommended sale (NIS) and pick the
    # LARGEST justified sale — the biggest satisfies every smaller requirement, so
    # tax + over-cap are never double-counted. Precedence breaks ties.
    _PRECEDENCE = {"thesis-break": 3, "risk-budget": 2, "catch-up": 1, "policy": 0}
    candidates: list[tuple[str, float]] = [("policy", tranche.tranche_nis)]
    if broken:
        candidates.append(("thesis-break", tranche.total_over_cap_nis))
    if risk_sale > 0:
        candidates.append(("risk-budget", risk_sale))
    wp_due, tr_exec = _resolve_catchup_inputs(session, user_id, today, tranche.tranche_nis)
    catchup_sale = catchup_sale_nis(
        waypoints_due=wp_due, tranches_executed=tr_exec,
        tranche_nis=tranche.tranche_nis, total_over_cap_nis=tranche.total_over_cap_nis,
    )
    if catchup_sale > 0:
        candidates.append(("catch-up", catchup_sale))
    category, recommended = max(candidates, key=lambda c: (c[1], _PRECEDENCE[c[0]]))

    notes_list: list[str] = list(_funnel_note)
    if rb is None:
        notes_list.append("The independent retirement-floor check is unavailable for this assessment.")
    elif not floor_attainable:
        notes_list.append("Liquid capital is below the retirement floor even before the modeled drawdown.")
    if thesis_unverified:
        notes_list.append(
            "NVDA thesis state could not be verified this run — holding the glide "
            "pace; re-checking next run (an unverified read is not treated as all-clear)."
        )
    if weakened:
        notes_list.append(
            "NVDA thesis WEAKENED — under review. Holding the glide pace for now "
            "(a weakened signal tightens monitoring, it does not by itself resize "
            "the position); watching for escalation."
        )

    if category == "thesis-break":
        headline = (
            f"NVDA thesis flagged BROKEN — accelerate the trim to your "
            f"{tranche.nvda_cap_pct:.0f}% cap now (~₪{recommended:,.0f}), not the "
            f"routine glide pace ({tranche.nvda_current_pct:.0f}% held today)."
        )
        tax_note, n_q = _TAX_NOTE_ACCEL, 1
    elif category == "risk-budget":
        headline = (
            f"The {_RISK_SHOCK*100:.0f}% NVDA stress test calls for a gross sale of "
            f"~₪{recommended:,.0f} under the retirement risk budget."
        )
        tax_note, n_q = _RISK_TAX_NOTE, 1
    elif category == "catch-up":
        headline = (
            f"NVDA deconcentration is BEHIND your glide schedule — catch up with a "
            f"~₪{recommended:,.0f} sell now (missed quarterly tranches), then resume the "
            "routine pace."
        )
        tax_note, n_q = _TAX_NOTE_ACCEL, 1
    else:  # policy
        headline = (
            f"Trim NVDA ~₪{recommended:,.0f} this quarter — "
            f"{tranche.nvda_current_pct:.0f}% is over your {tranche.nvda_cap_pct:.0f}% "
            f"cap; the over-cap position is spread over {tranche.n_quarters} quarters "
            "per your glide."
        )
        tax_note, n_q = _TAX_NOTE, tranche.n_quarters

    return NvdaPolicySell(
        status="sell_due", category=category,
        tranche_nis=round(recommended, 2),
        nvda_current_pct=tranche.nvda_current_pct,
        nvda_cap_pct=tranche.nvda_cap_pct,
        n_quarters=n_q,
        headline=headline,
        tax_note=tax_note,
        notes=tuple(notes_list),
    )


__all__ = ["NvdaPolicySell", "assess_nvda_policy_sell"]
