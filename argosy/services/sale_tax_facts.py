"""Resolve an authored sale into authoritative after-tax buying power.

This is the bridge between Argosy's existing tax engines and the canonical order
sheet.  It does not decide what to sell.  It prices the exact gross sale selected
by the author, including sourced commission, and refuses symbols whose basis/tax
provenance is not good enough to fund another order.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from argosy.services.after_tax import AfterTaxSale
from argosy.services.broker_fees import estimate as estimate_commission
from argosy.services.order_sheet import TaxImpact
from argosy.services.tax_simulation_ingest import realization_tax_summary

TAX_SIMULATION_MAX_AGE_DAYS = 90


def _parse_simulation_date(value: Any) -> date | None:
    raw = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def resolve_authoritative_sale(
    session: Any,
    *,
    user_id: str,
    symbol: str,
    gross_proceeds_usd: float,
    current_price_usd: float,
    as_of: datetime | None = None,
    tax_summary_fn: Callable[..., Any] = realization_tax_summary,
    commission_fn: Callable[[float], Any] = estimate_commission,
) -> AfterTaxSale:
    """Return executable net proceeds for an exact sale, or explicit failures.

    NVDA is currently the only position with an authoritative, ingested tax-lot
    simulation.  Generic ``lots`` rows are deliberately not used when their cost
    basis is absent/zero.  Adding another symbol requires a real tax provider,
    not a guessed 25% shortcut.
    """

    observed = as_of or datetime.now(UTC)
    ticker = symbol.strip().upper()
    if gross_proceeds_usd <= 0 or current_price_usd <= 0:
        return AfterTaxSale(
            failures=["gross proceeds and live current price must be positive"]
        )
    if ticker != "NVDA":
        return AfterTaxSale(
            failures=[
                f"{ticker}: no authoritative tax-lot provider; gross proceeds "
                "cannot be treated as buying power"
            ]
        )

    shares = gross_proceeds_usd / current_price_usd
    whole_shares = round(shares)
    # Authored USD notionals have cent precision; provider quotes can retain
    # sub-cent precision. Compare in dollars with half-cent rounding tolerance,
    # not a share epsilon that rejects our own rounded correction suggestion.
    if whole_shares < 1 or not math.isclose(
        gross_proceeds_usd, whole_shares * current_price_usd,
        rel_tol=0.0, abs_tol=0.005 + 1e-8,
    ):
        executable_gross = math.floor(shares) * current_price_usd
        return AfterTaxSale(
            failures=[
                f"{ticker}: venue requires whole shares; use an exact gross "
                f"notional such as ${executable_gross:,.2f} for "
                f"{math.floor(shares):,} shares"
            ]
        )
    shares = float(whole_shares)
    # Prefer the Section-102 eligible pool.  If it is insufficient, include the
    # exact remainder from breaking lots; the existing engine retains the two tax
    # regimes rather than blending them into a mental-model rate.
    eligible = tax_summary_fn(
        session,
        user_id,
        current_nvda_price_usd=current_price_usd,
        max_eligible_shares=shares,
        max_breaking_shares=0.0,
        as_of_date=observed.date(),
    )
    eligible_shares = min(float(getattr(eligible, "total_shares", 0.0) or 0.0), shares)
    aggregate = tax_summary_fn(
        session,
        user_id,
        current_nvda_price_usd=current_price_usd,
        max_eligible_shares=eligible_shares,
        max_breaking_shares=max(0.0, shares - eligible_shares),
        as_of_date=observed.date(),
    )
    if aggregate is None:
        return AfterTaxSale(failures=["NVDA: no ingested tax simulation is available"])

    failures: list[str] = []
    evidence_date = _parse_simulation_date(getattr(aggregate, "simulation_date", None))
    evidence_age_days: int | None = None
    evidence_expires_on: date | None = None
    if evidence_date is None:
        failures.append("tax simulation date is missing or unparseable")
    else:
        evidence_age_days = (observed.date() - evidence_date).days
        evidence_expires_on = evidence_date + timedelta(
            days=TAX_SIMULATION_MAX_AGE_DAYS
        )
        if evidence_age_days < 0:
            failures.append("tax simulation date is in the future")
        elif evidence_age_days > TAX_SIMULATION_MAX_AGE_DAYS:
            failures.append(
                "tax simulation is stale: "
                f"{evidence_age_days}d old exceeds {TAX_SIMULATION_MAX_AGE_DAYS}d"
            )
    covered = float(aggregate.total_shares or 0.0)
    incomplete = float(aggregate.incomplete_lot_shares or 0.0)
    gross = float(aggregate.gross_at_revalue_usd or 0.0)
    tax_usd = float(aggregate.embedded_tax_at_revalue_usd or 0.0)
    if abs(covered - shares) > 1e-6:
        failures.append(f"tax simulation covers {covered:.6f} of {shares:.6f} shares")
    if incomplete > 1e-6:
        failures.append(f"tax simulation has {incomplete:.6f} incomplete selected shares")
    if abs(gross - gross_proceeds_usd) > 1.0:
        failures.append(
            f"tax-engine gross ${gross:,.2f} does not match sale ${gross_proceeds_usd:,.2f}"
        )
    if tax_usd < 0 or tax_usd > gross_proceeds_usd:
        failures.append("tax-engine result is outside the sale proceeds")

    commission = commission_fn(gross_proceeds_usd)
    friction = float(commission.commission)
    net_before_costs = gross_proceeds_usd - tax_usd
    cost_basis = float(
        getattr(aggregate, "cost_basis_at_revalue_usd", 0.0) or 0.0
    )
    capital_income = float(
        getattr(aggregate, "capital_income_at_revalue_usd", 0.0) or 0.0
    )
    ordinary_income = float(
        getattr(aggregate, "ordinary_income_at_revalue_usd", 0.0) or 0.0
    )
    taxable_income = capital_income + ordinary_income
    tax = TaxImpact(
        gross_proceeds_usd=round(gross_proceeds_usd, 2),
        calculation_basis="trusted_tax_engine",
        cost_basis_usd=round(cost_basis, 2),
        taxable_gain_usd=round(taxable_income, 2),
        capital_income_usd=round(capital_income, 2),
        ordinary_income_usd=round(ordinary_income, 2),
        effective_tax_rate=(tax_usd / gross_proceeds_usd),
        estimated_tax_usd=round(tax_usd, 2),
        net_proceeds_usd=round(net_before_costs, 2),
        method=(
            "ingested Section-102/ESPP lot simulation "
            f"{aggregate.simulation_date}, revalued to live price; "
            f"commission: {commission.schedule.source}"
        ),
        authoritative=not failures,
        as_of=observed,
        evidence_as_of=evidence_date,
        evidence_age_days=evidence_age_days,
        evidence_max_age_days=TAX_SIMULATION_MAX_AGE_DAYS,
        evidence_expires_on=evidence_expires_on,
    )
    return AfterTaxSale(
        tax=tax,
        quantity=round(shares, 8),
        selected_lot_ids=[f"tax-simulation:{aggregate.simulation_date}"],
        friction_usd=round(friction, 2),
        net_fundable_usd=(
            round(net_before_costs - friction, 2) if not failures else None
        ),
        eligible=not failures,
        failures=failures,
    )


__all__ = ["TAX_SIMULATION_MAX_AGE_DAYS", "resolve_authoritative_sale"]
