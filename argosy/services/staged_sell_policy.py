"""Join the NVDA glide, actual-sale pace, and tax engine for one tranche.

The existing modules each answered part of the question: whether a policy sale
is due, where actual sales sit versus dated glide waypoints, and what an exact
sale nets after Section-102 tax.  This adapter carries those facts into the
unified deployment author.  It does not author the trade; it bounds the current
tranche and makes the author state when to execute and when to reassess.
"""

from __future__ import annotations

import math
from calendar import monthrange
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from argosy.services.nvda_policy_sell import assess_nvda_policy_sell
from argosy.services.nvda_sales_history import compute_nvda_sale_pace
from argosy.services.sale_tax_facts import resolve_authoritative_sale
from argosy.services.tax_simulation_ingest import eligible_shares


def build_nvda_staged_sell_policy(
    session: Any,
    *,
    user_id: str,
    current_price_usd: float,
    fx_usd_nis: float,
    current_nvda_value_usd: float | None = None,
    current_effective_nvda_value_usd: float | None = None,
    book_usd: float | None = None,
    as_of: datetime | None = None,
    assessment_fn: Callable[..., Any] = assess_nvda_policy_sell,
    pace_fn: Callable[..., Any] = compute_nvda_sale_pace,
    eligible_shares_fn: Callable[..., float | None] = eligible_shares,
    sale_resolver_fn: Callable[..., Any] = resolve_authoritative_sale,
) -> dict[str, Any] | None:
    """Return the executable boundary for NVDA's current staged tranche.

    ``None`` means the inputs cannot support a priced tranche.  A returned
    ``blocked`` policy preserves the reason and prevents gross proceeds from
    masquerading as deployable cash.
    """

    observed = as_of or datetime.now(UTC)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    if current_price_usd <= 0 or fx_usd_nis <= 0:
        return None

    assessment = assessment_fn(
        session=session,
        user_id=user_id,
        today=observed.date(),
    )
    if assessment.status != "sell_due" or float(assessment.tranche_nis or 0.0) <= 0:
        return {
            "symbol": "NVDA",
            "status": "no_action",
            "as_of": observed.isoformat(),
            "reason": assessment.headline,
        }

    pace = pace_fn(session, user_id, as_of=observed.date())
    waypoint_shares = int(
        getattr(pace, "shares_to_sell_by_waypoint", 0) or 0
    )
    # The dated glide is the canonical schedule. The legacy breach assessor
    # divided the total excess by a waypoint COUNT, which can disagree with the
    # actual (non-linear) waypoint weights. When a dated waypoint exists, size
    # this tranche from its exact share path; retain the assessor only as the
    # fallback when the plan has no share waypoint.
    uses_waypoint = bool(
        getattr(pace, "basis", "") == "glide"
        and getattr(pace, "next_waypoint_date", None)
        and waypoint_shares > 0
    )
    gross_usd = round(
        (
            waypoint_shares * float(current_price_usd)
            if uses_waypoint
            else float(assessment.tranche_nis) / float(fx_usd_nis)
        ),
        2,
    )
    deadline = pace.next_waypoint_date or date(observed.year, 12, 31)
    eligible_now = float(
        eligible_shares_fn(session, user_id, eligible=True) or 0.0
    )
    def _price_sale(amount_usd: float) -> Any:
        return sale_resolver_fn(
            session,
            user_id=user_id,
            symbol="NVDA",
            gross_proceeds_usd=amount_usd,
            current_price_usd=current_price_usd,
            as_of=observed,
        )

    # A weight waypoint is measured on the post-tax book. Selling shares moves
    # gross value into other assets, but tax/commission leave the portfolio and
    # shrink its denominator. The old share glide assumed a constant book and
    # therefore stopped short. Solve the exact gross iteratively through the
    # authoritative tax engine, rounding up to whole shares so the waypoint is
    # met rather than missed by a fraction.
    target_weight = getattr(pace, "next_waypoint_weight_pct", None)
    tax_adjusted = False
    if (
        uses_waypoint
        and target_weight is not None
        and float(current_nvda_value_usd or 0.0) > 0
        and float(book_usd or 0.0) > 0
    ):
        target_fraction = float(target_weight) / 100.0
        for _ in range(10):
            trial = _price_sale(gross_usd)
            if (
                not getattr(trial, "eligible", False)
                or getattr(trial, "tax", None) is None
            ):
                break
            tax_out = float(trial.tax.estimated_tax_usd) + float(
                getattr(trial, "friction_usd", 0.0) or 0.0
            )
            required = float(current_nvda_value_usd) - target_fraction * (
                float(book_usd) - tax_out
            )
            required_shares = max(1, math.ceil(required / current_price_usd))
            revised = round(required_shares * current_price_usd, 2)
            if abs(revised - gross_usd) <= 0.01:
                tax_adjusted = revised > round(
                    waypoint_shares * current_price_usd, 2
                )
                break
            gross_usd = revised
        tax_adjusted = gross_usd > round(
            waypoint_shares * current_price_usd, 2
        )

    full_waypoint_gross_usd = gross_usd
    full_waypoint_shares = max(1, int(round(full_waypoint_gross_usd / current_price_usd)))

    # The waypoint is a destination, not one executable block. Split it into
    # monthly clips; only the first is authorized now and every later clip must
    # be repriced/reviewed after prior fills and new evidence.
    clip_dates: list[date] = []
    cursor_year, cursor_month = observed.year, observed.month
    while True:
        cursor_month += 1
        if cursor_month == 13:
            cursor_year += 1
            cursor_month = 1
        if (cursor_year, cursor_month) >= (deadline.year, deadline.month):
            clip_dates.append(deadline)
            break
        clip_dates.append(
            date(
                cursor_year,
                cursor_month,
                min(deadline.day, monthrange(cursor_year, cursor_month)[1]),
            )
        )
    clip_count = max(1, len(clip_dates))
    base_clip, extra = divmod(full_waypoint_shares, clip_count)
    clip_shares = [base_clip + (1 if idx < extra else 0) for idx in range(clip_count)]
    clips = [
        {
            "sequence": idx + 1,
            "target_date": target.isoformat(),
            "shares": shares,
            "executable_now": idx == 0,
            "requires_reprice_and_reapproval": idx != 0,
        }
        for idx, (target, shares) in enumerate(zip(clip_dates, clip_shares, strict=True))
        if shares > 0
    ]
    current_clip_shares = clips[0]["shares"]
    gross_usd = round(current_clip_shares * current_price_usd, 2)
    priced = _price_sale(gross_usd)
    failures = list(getattr(priced, "failures", []) or [])
    actionable = bool(getattr(priced, "eligible", False)) and not failures
    tax = getattr(priced, "tax", None)
    tax_expiry = getattr(tax, "evidence_expires_on", None) if tax is not None else None
    execute_deadline = clip_dates[0]
    if tax_expiry is not None:
        execute_deadline = min(execute_deadline, tax_expiry)
        clips[0]["target_date"] = execute_deadline.isoformat()

    return {
        "symbol": "NVDA",
        "status": "actionable" if actionable else "blocked",
        "policy": "staged_glide_tranche",
        "as_of": observed.isoformat(),
        "category": assessment.category,
        "full_liquidation_forbidden": True,
        "reassess_after_fill": True,
        "recommended_current_tranche_usd": gross_usd,
        "maximum_current_tranche_usd": gross_usd,
        "recommended_current_tranche_shares": float(
            getattr(priced, "quantity", 0.0) or 0.0
        ),
        "current_price_usd": float(current_price_usd),
        "fx_usd_nis": float(fx_usd_nis),
        "gross_tranche_nis": round(gross_usd * float(fx_usd_nis), 2),
        "sizing_basis": (
            "after_tax_dated_glide_waypoint"
            if tax_adjusted
            else "canonical_dated_glide_waypoint"
            if uses_waypoint
            else "policy_equal_quarter_fallback"
        ),
        "glide_base_shares_to_sell_by_next_waypoint": waypoint_shares,
        "tax_denominator_adjustment_shares": max(
            0,
            full_waypoint_shares
            - waypoint_shares,
        ),
        "eligible_shares_now": eligible_now,
        "execute_no_later_than": execute_deadline.isoformat(),
        "next_review_date": execute_deadline.isoformat(),
        "remaining_glide_quarters": int(assessment.n_quarters),
        "current_weight_pct": float(assessment.nvda_current_pct),
        "current_direct_nvda_value_usd": current_nvda_value_usd,
        "current_effective_nvda_value_usd": current_effective_nvda_value_usd,
        "target_weight_pct": float(assessment.nvda_cap_pct),
        "pace_status": str(getattr(pace, "status", "unknown")),
        "tax_year": getattr(pace, "tax_year", None),
        "tax_year_target_shares": int(getattr(pace, "annual_flow", 0) or 0),
        "tax_year_sold_shares": int(
            getattr(pace, "sold_calendar_ytd", 0) or 0
        ),
        "schedule_target_shares_to_date": int(
            getattr(pace, "target_shares", 0) or 0
        ),
        "next_waypoint_date": (
            pace.next_waypoint_date.isoformat()
            if getattr(pace, "next_waypoint_date", None)
            else None
        ),
        "next_waypoint_weight_pct": getattr(
            pace, "next_waypoint_weight_pct", None
        ),
        "shares_to_sell_by_next_waypoint": int(
            full_waypoint_shares
        ),
        "clips": clips,
        "estimated_tax_usd": (
            float(tax.estimated_tax_usd) if tax is not None else None
        ),
        "estimated_net_proceeds_usd": (
            float(tax.net_proceeds_usd) if tax is not None else None
        ),
        "estimated_friction_usd": float(
            getattr(priced, "friction_usd", 0.0) or 0.0
        ),
        "estimated_net_fundable_usd": getattr(priced, "net_fundable_usd", None),
        "estimated_post_trade_weight_pct": (
            round(
                100.0
                * (float(current_nvda_value_usd) - gross_usd)
                / (
                    float(book_usd)
                    - float(tax.estimated_tax_usd)
                    - float(getattr(priced, "friction_usd", 0.0) or 0.0)
                ),
                4,
            )
            if tax is not None
            and float(current_nvda_value_usd or 0.0) > 0
            and float(book_usd or 0.0) > 0
            else None
        ),
        "estimated_post_trade_direct_nvda_weight_pct": (
            round(
                100.0
                * (float(current_nvda_value_usd) - gross_usd)
                / (
                    float(book_usd)
                    - float(tax.estimated_tax_usd)
                    - float(getattr(priced, "friction_usd", 0.0) or 0.0)
                ),
                4,
            )
            if tax is not None
            and float(current_nvda_value_usd or 0.0) > 0
            and float(book_usd or 0.0) > 0
            else None
        ),
        "estimated_post_trade_effective_nvda_weight_pct": (
            round(
                100.0
                * (float(current_effective_nvda_value_usd) - gross_usd)
                / (
                    float(book_usd)
                    - float(tax.estimated_tax_usd)
                    - float(getattr(priced, "friction_usd", 0.0) or 0.0)
                ),
                4,
            )
            if tax is not None
            and float(current_effective_nvda_value_usd or 0.0) > 0
            and float(book_usd or 0.0) > 0
            else None
        ),
        "tax_method": tax.method if tax is not None else None,
        "reason": (
            "Current clip only: trim "
            f"{int(round(float(getattr(priced, 'quantity', 0.0) or 0.0))):,} "
            "NVDA shares by "
            f"{execute_deadline.isoformat()} to advance toward the dated glide waypoint"
            + (
                f" at or below {float(pace.next_waypoint_weight_pct):.1f}%"
                if getattr(pace, "next_waypoint_weight_pct", None) is not None
                else ""
            )
            + (
                "; this includes the extra shares required because realized tax "
                "shrinks the post-sale portfolio denominator"
                if tax_adjusted
                else ""
            )
            + f"; {full_waypoint_shares:,} shares remain the total target by "
            + f"{deadline.isoformat()}, split across {clip_count} dated clips; "
            + "reassess after every fill before authoring the next clip."
            if uses_waypoint
            else assessment.headline
        ),
        "tax_note": assessment.tax_note,
        "notes": list(assessment.notes),
        "failures": failures,
    }


__all__ = ["build_nvda_staged_sell_policy"]
