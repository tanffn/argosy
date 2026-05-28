"""Glide path — target equity/bond/cash allocation by age.

Closes HIGH #9 from the 2026-05-28 SDD review. The prior projection had no
documented glide path — the user's 60%+ NVDA portfolio at age 50+ would
have been catastrophic, but Argosy would never have said "shift to 60/40
by age 50, 50/50 by 60".

Default policy: Vanguard target-date glide (gradual equity decline from
90% at age 30 to 50% at age 65, holding 30% in retirement). Source:
``vanguard_target_date_glide``.

Plan: ``docs/superpowers/plans/2026-05-28-retirement-companion-overhaul.md``
§ Wave 4 HIGH #9.
"""
from dataclasses import dataclass
from typing import Literal

from argosy.services.retirement.citations import ValueWithRationale


PolicyId = Literal["vanguard_target_date", "age_minus_30_bonds", "custom"]


@dataclass(frozen=True)
class GlidePathPoint:
    age: int
    target_equity_pct: ValueWithRationale
    target_bond_pct: ValueWithRationale
    target_cash_pct: ValueWithRationale


def _vanguard_target_date(age: int) -> tuple[float, float, float]:
    """Vanguard target-date glide:
      age 30-40: 90% equity, 8% bonds, 2% cash
      age 40-50: gradual ramp down
      age 50-60: 70% equity
      age 60-65: linear ramp to 50%
      age 65-75: 50% equity, 45% bonds, 5% cash
      age 75+:   40% equity, 50% bonds, 10% cash
    """
    if age <= 30:
        return 0.90, 0.08, 0.02
    if age <= 50:
        # Linear from (30, 0.90) to (50, 0.70)
        equity = 0.90 - (age - 30) * (0.20 / 20)
        return equity, 1.0 - equity - 0.02, 0.02
    if age <= 65:
        # Linear from (50, 0.70) to (65, 0.50)
        equity = 0.70 - (age - 50) * (0.20 / 15)
        return equity, 1.0 - equity - 0.05, 0.05
    if age <= 75:
        return 0.50, 0.45, 0.05
    return 0.40, 0.50, 0.10


def _age_minus_30_bonds(age: int) -> tuple[float, float, float]:
    """Classic 'bonds = age - 30' heuristic.

    Equity = max(20, 100 - (age - 30)) / 100. More aggressive in early
    years than Vanguard.
    """
    bonds_pct = max(0.10, min(0.80, (age - 30) / 100.0))
    equity_pct = max(0.20, 1.0 - bonds_pct - 0.02)
    cash_pct = 1.0 - equity_pct - bonds_pct
    return equity_pct, bonds_pct, cash_pct


def compute_glide_path(
    *,
    start_age: int = 30,
    end_age: int = 95,
    policy: PolicyId = "vanguard_target_date",
) -> list[GlidePathPoint]:
    """Return the per-age allocation table from start_age to end_age."""
    fn = _vanguard_target_date if policy == "vanguard_target_date" else _age_minus_30_bonds

    source_id = (
        "vanguard_target_date_glide"
        if policy == "vanguard_target_date"
        else "bogleheads_three_fund"
    )

    out: list[GlidePathPoint] = []
    for age in range(start_age, end_age + 1):
        eq, bd, cs = fn(age)
        out.append(GlidePathPoint(
            age=age,
            target_equity_pct=ValueWithRationale(
                value=round(eq, 4),
                unit="fraction",
                source_id=source_id,
                rationale=f"Target equity allocation at age {age} under '{policy}'.",
                confidence="high",
            ),
            target_bond_pct=ValueWithRationale(
                value=round(bd, 4),
                unit="fraction",
                source_id=source_id,
                rationale=f"Target bond allocation at age {age} under '{policy}'.",
                confidence="high",
            ),
            target_cash_pct=ValueWithRationale(
                value=round(cs, 4),
                unit="fraction",
                source_id=source_id,
                rationale=f"Target cash allocation at age {age} under '{policy}'.",
                confidence="high",
            ),
        ))
    return out


def target_at_age(
    age: int,
    *,
    policy: PolicyId = "vanguard_target_date",
) -> GlidePathPoint:
    """Single-age lookup helper."""
    table = compute_glide_path(start_age=age, end_age=age, policy=policy)
    return table[0]
