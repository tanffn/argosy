"""Policy-aware retire-ready detection (Wave 8 v2.3, codex deep-audit #1).

The existing ``detect_retire_ready`` in ``argosy.services.cashflow_projection``
implements a single, implicit policy: "first month where projected
portfolio real-return + pension annuity covers expenses." That's a
sensible *capital-preservation* reading — you never touch principal —
but it is NOT the reading used in the user's actual plan document
(``Jacobs_Wealth_Plan.md``), which is explicit about a Bengen-style
**Safe Withdrawal Rate (SWR)** framework at 3.5%.

These two readings produce materially different ages. The capital-
preservation reading typically lands earlier because expected real
return (~5-6% after tax + inflation) exceeds the 3.5% SWR floor. So
the recap headline that currently says "retire at 44" is reading-1; the
plan-document number "retire at 51" is reading-2. Both can be right;
the user just needs both surfaced.

This module is the policy switch. It produces a ``ReadinessVerdict``
per policy, pure-function, no DB. The caller (``plan_headline.
compute_recap_summary``) runs all three and surfaces them as a list so
the UI can render "Earliest by returns-only: 44 • Earliest by SWR 3.5%:
51 • Earliest by SWR 4%: 48".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

from argosy.services.cashflow_projection import CashflowPoint

ReadinessPolicy = Literal["returns_only", "swr_3_5", "swr_4_0"]

_SWR_RATE_BY_POLICY: dict[ReadinessPolicy, float] = {
    "swr_3_5": 0.035,
    "swr_4_0": 0.040,
}


@dataclass(frozen=True)
class ReadinessVerdict:
    """One policy's reading of the cashflow series.

    ``retire_ready_age`` / ``retire_ready_months_out`` are None when the
    policy never crosses within the series horizon. Rationale always
    carries a plain-English explanation of WHY the verdict came out the
    way it did, suitable for direct display in the recap UI.
    """

    policy: ReadinessPolicy
    retire_ready_age: float | None
    retire_ready_months_out: int | None
    rationale: str


def _detect_returns_only(
    series: Sequence[CashflowPoint],
) -> tuple[int, float] | None:
    """First tick where ``portfolio_income_base + annuity >= expenses``."""
    for p in series:
        total = p.portfolio_income_base_monthly_nis + p.pension_annuity_monthly_nis
        if total >= p.expenses_monthly_nis:
            return p.months_out, p.age_years
    return None


def _detect_swr(
    series: Sequence[CashflowPoint],
    *,
    rate_annual: float,
) -> tuple[int, float] | None:
    """First tick where ``portfolio_value_base * rate / 12 + annuity >= expenses``."""
    monthly_rate = rate_annual / 12.0
    for p in series:
        swr_income = p.portfolio_value_base_nis * monthly_rate
        total = swr_income + p.pension_annuity_monthly_nis
        if total >= p.expenses_monthly_nis:
            return p.months_out, p.age_years
    return None


def _rationale_crossed(
    *,
    policy: ReadinessPolicy,
    age: float,
    months_out: int,
    target_annual_spend_nis: float,
    current_portfolio_value_nis: float,
) -> str:
    if policy == "returns_only":
        return (
            f"Portfolio's real return plus pension annuity first covers "
            f"monthly expenses at age {age:.1f} ({months_out} months out). "
            f"Capital-preservation reading: principal is never touched."
        )
    rate = _SWR_RATE_BY_POLICY[policy]
    multiple = (1.0 / rate) if rate > 0 else float("inf")
    return (
        f"Portfolio reaches the SWR-{rate:.1%} threshold at age {age:.1f} "
        f"({months_out} months out). At a {rate:.1%} withdrawal rate, the "
        f"portfolio must be ~{multiple:.1f}x annual spend "
        f"(~{target_annual_spend_nis * multiple:,.0f} NIS) net of annuity. "
        f"Starting value {current_portfolio_value_nis:,.0f} NIS."
    )


def _rationale_never_crossed(
    *,
    policy: ReadinessPolicy,
    horizon_months: int,
    target_annual_spend_nis: float,
) -> str:
    if policy == "returns_only":
        return (
            f"Returns-only reading never crosses within the projected "
            f"{horizon_months}-month horizon — portfolio real return + annuity "
            f"stays below monthly expenses throughout."
        )
    rate = _SWR_RATE_BY_POLICY[policy]
    multiple = (1.0 / rate) if rate > 0 else float("inf")
    return (
        f"SWR-{rate:.1%} reading never crosses within the projected "
        f"{horizon_months}-month horizon — portfolio doesn't reach the "
        f"~{multiple:.1f}x spend multiple (~{target_annual_spend_nis * multiple:,.0f} NIS) "
        f"net of annuity in the projection window."
    )


def detect_retire_ready_by_policy(
    series: Sequence[CashflowPoint],
    *,
    policy: ReadinessPolicy,
    current_portfolio_value_nis: float,
    target_annual_spend_nis: float,
) -> ReadinessVerdict:
    """Per-policy retire-ready detection."""
    if policy == "returns_only":
        hit = _detect_returns_only(series)
    elif policy in _SWR_RATE_BY_POLICY:
        hit = _detect_swr(series, rate_annual=_SWR_RATE_BY_POLICY[policy])
    else:  # pragma: no cover
        raise ValueError(f"Unknown ReadinessPolicy: {policy!r}")

    if hit is None:
        return ReadinessVerdict(
            policy=policy,
            retire_ready_age=None,
            retire_ready_months_out=None,
            rationale=_rationale_never_crossed(
                policy=policy,
                horizon_months=len(series),
                target_annual_spend_nis=target_annual_spend_nis,
            ),
        )

    months_out, age = hit
    return ReadinessVerdict(
        policy=policy,
        retire_ready_age=age,
        retire_ready_months_out=months_out,
        rationale=_rationale_crossed(
            policy=policy,
            age=age,
            months_out=months_out,
            target_annual_spend_nis=target_annual_spend_nis,
            current_portfolio_value_nis=current_portfolio_value_nis,
        ),
    )


def detect_retire_ready_all_policies(
    series: Sequence[CashflowPoint],
    *,
    current_portfolio_value_nis: float,
    target_annual_spend_nis: float,
) -> list[ReadinessVerdict]:
    """Run every v1 policy in stable order (returns_only -> swr_3_5 -> swr_4_0)."""
    policies: list[ReadinessPolicy] = ["returns_only", "swr_3_5", "swr_4_0"]
    return [
        detect_retire_ready_by_policy(
            series,
            policy=p,
            current_portfolio_value_nis=current_portfolio_value_nis,
            target_annual_spend_nis=target_annual_spend_nis,
        )
        for p in policies
    ]
