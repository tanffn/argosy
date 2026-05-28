"""Phase-based expense curve — moves beyond flat × inflation.

Closes HIGH #14 + MEDs #21 (IDF service) + #22 (healthcare) from the
2026-05-28 SDD review. Prior projection inflated current burn flat per
year. Reality: kids' costs peak in their teen years; empty-nest dip
follows; healthcare ramps post-65; LTC tail late.

Each phase applies a monthly_multiplier vs the user's current burn,
plus an inflation_premium (extra %/yr above CPI).

Plan: ``docs/superpowers/plans/2026-05-28-retirement-companion-overhaul.md``
§ Wave 4 HIGH #14 + MEDs #21, #22.
"""
from dataclasses import dataclass

from argosy.services.retirement.citations import ValueWithRationale


@dataclass(frozen=True)
class ExpensePhase:
    start_age: int
    end_age: int
    label: str
    monthly_multiplier: ValueWithRationale
    inflation_premium: ValueWithRationale  # extra %/yr above CPI


def build_phase_expense_curve(
    *,
    has_kids: bool = True,
    kids_birth_years: list[int] | None = None,
    healthcare_ramp_age: int = 65,
) -> list[ExpensePhase]:
    """Return the user's projected expense phases by age.

    Phases:
      kids_peak: kids 12-22 (high expenses; lessons / college / car)
      empty_nest: kids 22-30 (dip; ~85% of baseline)
      pre_healthcare: 50-65 (1.0× baseline + small inflation premium for prep)
      healthcare_ramp: 65-80 (1.10× + 1.5%/yr above CPI)
      late_life: 80-95 (1.15× + 3%/yr above CPI; LTC tail)

    For Israeli households with kids of military-service age (kid_birth_year
    + 18 to + 21), the IDF service phase nets the household ~5% expense
    reduction (kid is fed + housed by IDF) — handled separately by
    ``idf_service_phases()``.
    """
    phases: list[ExpensePhase] = []

    def _wrap_mult(v: float, label: str, source: str) -> ValueWithRationale:
        return ValueWithRationale(
            value=v,
            unit="fraction",
            source_id=source,
            rationale=f"Monthly-expense multiplier vs baseline burn during {label}.",
            confidence="medium",
        )

    def _wrap_premium(v: float, label: str) -> ValueWithRationale:
        return ValueWithRationale(
            value=v,
            unit="fraction",
            source_id="argosy_derived",
            rationale=(
                f"Extra annual inflation premium during {label} (above CPI). "
                "Models cohort-specific cost growth (e.g. healthcare 1-3% real)."
            ),
            confidence="medium",
        )

    # Kids high-cost phase
    if has_kids:
        phases.append(ExpensePhase(
            start_age=43,
            end_age=55,
            label="kids_peak",
            monthly_multiplier=_wrap_mult(
                1.10, "kids peak (lessons + college prep)", "argosy_derived",
            ),
            inflation_premium=_wrap_premium(0.005, "kids peak"),
        ))
        phases.append(ExpensePhase(
            start_age=56,
            end_age=64,
            label="empty_nest",
            monthly_multiplier=_wrap_mult(
                0.85, "empty nest", "argosy_derived",
            ),
            inflation_premium=_wrap_premium(0.0, "empty nest"),
        ))

    # Healthcare ramp (post-65)
    phases.append(ExpensePhase(
        start_age=healthcare_ramp_age,
        end_age=80,
        label="healthcare_ramp",
        monthly_multiplier=_wrap_mult(
            1.10, "healthcare ramp", "argosy_derived",
        ),
        inflation_premium=_wrap_premium(0.015, "healthcare ramp (1.5%/yr above CPI)"),
    ))
    phases.append(ExpensePhase(
        start_age=81,
        end_age=95,
        label="late_life_ltc",
        monthly_multiplier=_wrap_mult(
            1.15, "late life + LTC tail", "argosy_derived",
        ),
        inflation_premium=_wrap_premium(0.03, "late life (3%/yr above CPI; LTC tail)"),
    ))

    return phases


def idf_service_phases(
    *,
    kids_birth_years: list[int] | None = None,
    service_start_age: int = 18,
    service_end_age: int = 21,
    expense_reduction_pct: float = 0.05,
) -> list[ExpensePhase]:
    """Per-child IDF service phase (closes MED #21).

    When a kid is in IDF service, the household covers slightly less of
    the kid's costs (housing + food + clothing handled by IDF). Models
    as a ``expense_reduction_pct`` (default 5%) multiplier on baseline
    burn during the service window.
    """
    if not kids_birth_years:
        return []
    phases: list[ExpensePhase] = []
    for i, birth_year in enumerate(kids_birth_years, start=1):
        start_age = birth_year + service_start_age
        end_age = birth_year + service_end_age
        phases.append(ExpensePhase(
            start_age=start_age,
            end_age=end_age,
            label=f"kid_{i}_idf_service",
            monthly_multiplier=ValueWithRationale(
                value=round(1.0 - expense_reduction_pct, 4),
                unit="fraction",
                source_id="argosy_derived",
                rationale=(
                    f"Kid #{i} in IDF service (ages {service_start_age}-{service_end_age}). "
                    f"Household burn reduces by ~{expense_reduction_pct*100:.0f}% as IDF "
                    "covers housing, food, basic clothing."
                ),
                confidence="medium",
            ),
            inflation_premium=ValueWithRationale(
                value=0.0,
                unit="fraction",
                source_id="argosy_derived",
                rationale="No premium during IDF service phase.",
                confidence="high",
            ),
        ))
    return phases
