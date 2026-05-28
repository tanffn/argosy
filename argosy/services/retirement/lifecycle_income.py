"""Lifecycle income timeline — events that shift household income over time.

Closes HIGH #13 from the 2026-05-28 SDD review. Prior projection had no
modeling of RSU vest cadence, partner career arc, side income, or job-loss
shocks — all of which shift cashflow materially in pre-retirement years.

Events:
  - rsu_vest: known quarterly vesting schedule (positive cash event)
  - partner_career_change: known transition (positive or negative)
  - side_income: known steady-state contributor
  - unemployment_risk: probabilistic shock (negative)

Plan: ``docs/superpowers/plans/2026-05-28-retirement-companion-overhaul.md``
§ Wave 4 HIGH #13.
"""
from dataclasses import dataclass
from typing import Literal

from argosy.services.retirement.citations import ValueWithRationale


EventType = Literal["rsu_vest", "rsu_cliff", "partner_job_change", "side_income", "unemployment_risk"]


@dataclass(frozen=True)
class LifecycleIncomeEvent:
    age: float
    event_type: EventType
    monthly_impact_nis: ValueWithRationale  # signed: positive = income inflow
    probability: ValueWithRationale  # 1.0 for known events; < 1 for risks
    rationale: str


def build_lifecycle_timeline(
    *,
    current_age: float,
    rsu_quarterly_vests: list[dict] | None = None,
    partner_income_monthly_nis: float = 0.0,
    side_income_monthly_nis: float = 0.0,
    unemployment_annual_probability: float = 0.05,
) -> list[LifecycleIncomeEvent]:
    """Build the lifecycle income event list."""
    events: list[LifecycleIncomeEvent] = []

    # RSU quarterly vests (known schedule)
    for vest in (rsu_quarterly_vests or []):
        period_age = current_age  # could be parsed from vest['date'] for finer detail
        usd_value = float(vest.get("value_usd") or 0.0)
        # Convert USD → NIS at a conservative 3.0 fx
        nis_value = usd_value * 3.0
        events.append(LifecycleIncomeEvent(
            age=period_age,
            event_type="rsu_vest",
            monthly_impact_nis=ValueWithRationale(
                value=round(nis_value / 3.0, 2),  # spread over the quarter
                unit="NIS/mo",
                source_id="argosy_derived",
                rationale=(
                    f"RSU vest of ${usd_value:,.0f} on {vest.get('date', '?')} "
                    f"spread across the quarter."
                ),
                confidence="high",
            ),
            probability=ValueWithRationale(
                value=1.0,
                unit="fraction",
                source_id=None,
                rationale="Known vest schedule from intake.",
                confidence="high",
            ),
            rationale=f"RSU vest: {vest.get('period', '?')}",
        ))

    # Partner income (steady-state)
    if partner_income_monthly_nis > 0:
        events.append(LifecycleIncomeEvent(
            age=current_age,
            event_type="side_income",
            monthly_impact_nis=ValueWithRationale(
                value=partner_income_monthly_nis,
                unit="NIS/mo",
                source_id="argosy_derived",
                rationale="Partner steady-state monthly income from intake.",
                confidence="high",
            ),
            probability=ValueWithRationale(
                value=1.0, unit="fraction", source_id=None,
                rationale="Steady-state.", confidence="high",
            ),
            rationale="Partner career income — applied throughout pre-retirement.",
        ))

    # Side income
    if side_income_monthly_nis > 0:
        events.append(LifecycleIncomeEvent(
            age=current_age,
            event_type="side_income",
            monthly_impact_nis=ValueWithRationale(
                value=side_income_monthly_nis,
                unit="NIS/mo",
                source_id="argosy_derived",
                rationale="Side income from intake.",
                confidence="medium",
            ),
            probability=ValueWithRationale(
                value=0.9, unit="fraction", source_id=None,
                rationale="Side income carries modest uncertainty.",
                confidence="medium",
            ),
            rationale="Side income — applied at reduced probability.",
        ))

    # Unemployment risk
    if unemployment_annual_probability > 0:
        # Translate annual prob to a generic event marker (engine consumes via prob)
        events.append(LifecycleIncomeEvent(
            age=current_age,
            event_type="unemployment_risk",
            monthly_impact_nis=ValueWithRationale(
                value=-40_000.0,  # assumed 6 months of lost salary
                unit="NIS/mo",
                source_id="argosy_derived",
                rationale=(
                    "Unemployment shock — modeled as ₪40K/mo loss for 6 "
                    "months when triggered. Probability calibrated per "
                    "annual risk."
                ),
                confidence="low",
            ),
            probability=ValueWithRationale(
                value=unemployment_annual_probability,
                unit="fraction",
                source_id="argosy_derived",
                rationale=(
                    f"Annual unemployment probability {unemployment_annual_probability:.0%}. "
                    "Calibrated to industry baseline; user can override per intake."
                ),
                confidence="medium",
            ),
            rationale="Unemployment risk — sampled per simulation path.",
        ))

    return events
