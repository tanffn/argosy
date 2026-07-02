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

Read-only: it NEVER writes a proposal or executes. Reuses the codex-verified
breach-tranche money-math and the existing thesis-flag loader.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from argosy.logging import get_logger
from argosy.services.breach_router import compute_breach_tranche
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


@dataclass(frozen=True)
class NvdaPolicySell:
    status: str            # "sell_due" | "no_action"
    category: str          # "policy" | "thesis-break" | (risk-budget / catch-up later)
    tranche_nis: float     # the RECOMMENDED sale this period (not always a /n tranche)
    nvda_current_pct: float
    nvda_cap_pct: float
    n_quarters: int
    headline: str
    tax_note: str
    notes: tuple[str, ...] = field(default_factory=tuple)


def _resolve_risk_budget_inputs(session, user_id: str):
    """(net_worth_nis, nvda_value_nis, perpetuity_base_nis) for the risk-budget
    check, or ``None`` when unavailable — the same lightweight resolver the breach
    router uses. Best-effort: missing inputs → ``None`` → no risk-budget sell (never
    over-sell on missing data)."""
    try:
        from sqlalchemy import desc, select

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
            return None
        nums = resolve_plan_numbers(session, user_id=user_id, decision_run_id=int(drun))

        def _v(key):
            n = nums.get(key)
            if n is None or getattr(n, "status", None) != "resolved" or not n.value:
                return None
            return float(n.value)

        nw = _v("portfolio.net_worth_nis")
        nvda = _v("portfolio.nvda_value_nis")
        floor = _v("fi.perpetuity_base_nis")
        if nw is None or nvda is None or floor is None:
            return None
        return nw, nvda, floor
    except Exception:  # noqa: BLE001 — best-effort; absence → no risk-budget sell
        _log.warning("nvda_sell.risk_budget_inputs_failed", user_id=user_id)
        return None


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
    rb = _resolve_risk_budget_inputs(session, user_id)
    if rb is not None:
        nw, nvda_val, floor = rb
        risk_sale = risk_budget_sale_nis(
            net_worth_nis=nw, nvda_value_nis=nvda_val, perpetuity_base_nis=floor,
            shock=_RISK_SHOCK,
        )

    if tranche is None:
        # Within cap: policy/thesis-break have nothing over-cap to trim. Only a
        # risk-budget breach can require a sell here.
        if risk_sale <= 0:
            return NvdaPolicySell(
                status="no_action", category="policy", tranche_nis=0.0,
                nvda_current_pct=0.0, nvda_cap_pct=0.0, n_quarters=0,
                headline=(
                    "NVDA is within its concentration cap and a plausible drawdown "
                    "leaves your retirement floor intact — no sell is due this period."
                ),
                tax_note="",
            )
        nw, nvda_val, _floor = rb
        cur_pct = round(nvda_val / nw * 100.0, 2) if nw > 0 else 0.0
        return NvdaPolicySell(
            status="sell_due", category="risk-budget", tranche_nis=round(risk_sale, 2),
            nvda_current_pct=cur_pct, nvda_cap_pct=0.0, n_quarters=1,
            headline=(
                f"NVDA is within its cap, but a {_RISK_SHOCK*100:.0f}% NVDA drawdown "
                f"would breach your safe-retirement floor — sell ~₪{risk_sale:,.0f} now "
                "to bring the portfolio back inside the risk budget."
            ),
            tax_note=_TAX_NOTE_ACCEL,
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
    category, recommended = max(candidates, key=lambda c: (c[1], _PRECEDENCE[c[0]]))

    notes_list: list[str] = []
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
            f"NVDA concentration threatens your safe-retirement floor under a "
            f"{_RISK_SHOCK*100:.0f}% NVDA drawdown — sell ~₪{recommended:,.0f} now to "
            f"bring the portfolio back inside the risk budget (ahead of the routine glide)."
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
