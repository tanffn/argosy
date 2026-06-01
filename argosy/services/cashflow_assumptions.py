"""Default cashflow-projection assumptions for the /plan recap (Wave 8 Piece C).

Provides six per-field defaults the recap's cashflow sliders should
pre-populate with, each carrying a ``source`` ("sigma_calibrator",
"goals_yaml", "plan_baseline", or "default") and a markdown
rationale the UI can render as a `▸ why?` tooltip.

Codex zigzag round 1 narrowed this to deterministic v1: NO
synthesizer-posture-string interpretation. Three sources only:

  1. Sigma calibrator (``argosy.services.retirement.sigma_calibration``)
     — produces a portfolio-weighted σ. Used for ``sigma_annual``.
  2. ``UserContext.goals_yaml`` — user-stated values for
     ``tax_rate_pct``, ``retirement_target_age``, ``lifestyle_drift_annual``.
  3. Hardcoded fallback with rationale — every field has one.

Per-field fallback semantics are baked into the constants below so a
test pin can reference them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import yaml
from sqlalchemy.orm import Session

from argosy.state.models import UserContext


# Fallback constants (hardcoded defaults per the spec).
DEFAULT_MU_NOMINAL_ANNUAL = 0.08
DEFAULT_SIGMA_ANNUAL = 0.18
DEFAULT_TAX_RATE = 0.25
DEFAULT_INFLATION_ANNUAL = 0.025
DEFAULT_RETIREMENT_AGE = 49.0
DEFAULT_LIFESTYLE_DRIFT_ANNUAL = 0.0


# Rationale strings — copied verbatim from the wave-8 spec doc.
RATIONALE_MU = (
    "Long-run real-equity expected return; conservative side of "
    "7-10% historical range. Override with your own number if you "
    "have a specific portfolio view."
)
RATIONALE_SIGMA_DEFAULT = (
    "Unweighted-equity default; portfolio-specific calibration "
    "unavailable."
)
RATIONALE_TAX_DEFAULT = (
    "Israeli CGT marginal rate at user's bracket. Adjust if your "
    "effective rate is different."
)
RATIONALE_INFLATION = "Bank of Israel long-run target."
RATIONALE_RETIREMENT_DEFAULT = (
    "Default FIRE target. Override to model what-ifs at other ages."
)
RATIONALE_LIFESTYLE_DEFAULT = (
    "Conservative default — matches goals_yaml "
    "`lifestyle_aspirations_note` when the user expects flat real "
    "spend. Override to model gradual spending growth."
)


AssumptionSource = Literal[
    "sigma_calibrator", "goals_yaml", "plan_baseline", "default"
]


@dataclass(frozen=True)
class AssumptionField:
    value: float
    source: AssumptionSource
    rationale_md: str


@dataclass(frozen=True)
class DefaultAssumptionsResponse:
    mu_nominal_annual: AssumptionField
    sigma_annual: AssumptionField
    tax_rate: AssumptionField
    inflation_annual: AssumptionField
    retirement_age: AssumptionField
    lifestyle_drift_annual: AssumptionField


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_goals_yaml(
    session: Session | None, user_id: str
) -> dict:
    """Pull ``UserContext.goals_yaml`` and parse. Returns {} on any
    error (missing context, empty yaml, parse failure) so callers can
    just ``.get(key)`` without guarding."""
    if session is None:
        return {}
    try:
        ctx = (
            session.query(UserContext)
            .filter(UserContext.user_id == user_id)
            .one_or_none()
        )
    except Exception:  # pragma: no cover - defensive
        return {}
    if ctx is None or not ctx.goals_yaml:
        return {}
    try:
        loaded = yaml.safe_load(ctx.goals_yaml)
    except yaml.YAMLError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


def _coerce_float(v: object) -> float | None:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _resolve_mu_nominal(
    session: Session | None, user_id: str
) -> AssumptionField:
    """Resolve nominal expected portfolio return.

    Priority order (codex deep-audit #3):
      1. Plan baseline raw_markdown — search for "Real return: X%"
         pattern and convert to nominal via inflation default. This
         binds the recap's μ to what the user's plan actually
         assumes, rather than overriding it with the hardcoded 8%.
      2. goals_yaml.mu_nominal_annual when explicitly set.
      3. Hardcoded 0.08 default.

    Real → Nominal conversion uses DEFAULT_INFLATION_ANNUAL (2.5%),
    matching the plan's own framework. Plans that emit nominal
    directly should set goals_yaml.mu_nominal_annual.
    """
    plan_mu = _try_extract_mu_from_plan(session, user_id)
    if plan_mu is not None:
        return plan_mu
    if session is None:
        return AssumptionField(
            value=DEFAULT_MU_NOMINAL_ANNUAL,
            source="default",
            rationale_md=RATIONALE_MU,
        )
    return AssumptionField(
        value=DEFAULT_MU_NOMINAL_ANNUAL,
        source="default",
        rationale_md=RATIONALE_MU,
    )


def _try_extract_mu_from_plan(
    session: Session | None, user_id: str
) -> AssumptionField | None:
    """Best-effort parse of the user's baseline plan raw_markdown
    for a "Real return: X%" line. Returns None when the plan is
    absent or the pattern doesn't match.

    Pattern: matches "real return" (case-insensitive) followed by
    a numeric percentage like "4.5%" or "4.5 %", optionally with
    "per year" / "/yr" trailer. Converts real → nominal at the
    DEFAULT_INFLATION_ANNUAL rate (Bank of Israel 2.5%).
    """
    if session is None:
        return None
    try:
        import re

        from sqlalchemy import desc, select

        from argosy.state.models import PlanVersion

        plan = (
            session.execute(
                select(PlanVersion)
                .where(
                    PlanVersion.user_id == user_id,
                    PlanVersion.role == "baseline",
                )
                .order_by(desc(PlanVersion.imported_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        if plan is None or not plan.raw_markdown:
            return None
        # Match: "Real return: 4.5%" or "Real return - 4.5 %" or
        # "real return is 4.5% per year". Case-insensitive.
        m = re.search(
            r"real\s+return[^0-9]{0,20}(\d+(?:\.\d+)?)\s*%",
            plan.raw_markdown,
            flags=re.IGNORECASE,
        )
        if m is None:
            return None
        real_pct = float(m.group(1))
        if not (0.0 <= real_pct <= 20.0):
            return None
        real_decimal = real_pct / 100.0
        nominal = real_decimal + DEFAULT_INFLATION_ANNUAL
        return AssumptionField(
            value=nominal,
            source="plan_baseline",
            rationale_md=(
                f"From your baseline plan: real return {real_pct:.1f}% "
                f"per year. Adding inflation "
                f"({DEFAULT_INFLATION_ANNUAL * 100:.1f}%) gives nominal μ = "
                f"{nominal * 100:.1f}%. This is the conservative-side "
                f"figure the plan itself uses for projections; the "
                f"recap's cashflow projection inherits it so the "
                f"recap and the plan don't disagree on what return to "
                f"expect."
            ),
        )
    except Exception:  # pragma: no cover - defensive
        return None


def _resolve_sigma(
    session: Session | None, user_id: str
) -> AssumptionField:
    """Best-effort calibrate σ from the user's portfolio. Falls back
    to the unweighted default + a "calibration unavailable" rationale
    when the calibrator can't produce a portfolio-aware figure.

    Wave 8 v2 polish: rationale text is rewritten in plain English so
    the user understands what σ actually MEANS for outcomes — not
    just the number.
    """
    if session is None:
        return AssumptionField(
            value=DEFAULT_SIGMA_ANNUAL,
            source="default",
            rationale_md=RATIONALE_SIGMA_DEFAULT,
        )
    try:
        from argosy.services.retirement.sigma_calibration import (
            calibrate_sigma_from_holdings,
        )

        cal = calibrate_sigma_from_holdings(user_id=user_id, session=session)
        sigma_value = float(cal.sigma_annual.value)
        if sigma_value <= 0:
            raise ValueError("non-positive sigma")
        sigma_pct = sigma_value * 100.0
        default_pct = DEFAULT_SIGMA_ANNUAL * 100.0
        # Plain-English risk band — "safe" is the wrong word for σ
        # (volatility is risk, not safety) so we frame it as
        # year-to-year swing magnitude.
        if sigma_pct < 15:
            risk_band = (
                "lower than a typical all-stocks portfolio — "
                "year-to-year swings should be modest"
            )
        elif sigma_pct < 22:
            risk_band = (
                "in the diversified-equity range — a normal year might "
                "swing the portfolio about ±20% top to bottom"
            )
        elif sigma_pct < 30:
            risk_band = (
                "elevated compared to a fully diversified portfolio — "
                "this reflects meaningful single-name or sector "
                "exposure; expect bigger drawdowns in bad years"
            )
        else:
            risk_band = (
                "high — this number reflects heavy concentration "
                "(typically a single stock that's a large share of the "
                "portfolio). Bad years can lose 30-50% before recovering. "
                "The plan's diversification glidepath is designed to "
                "bring this down over time"
            )
        rationale = (
            f"Portfolio-weighted volatility from your latest snapshot. "
            f"σ = {sigma_pct:.1f}% per year — {risk_band}. "
            f"For reference, a diversified S&P-500 portfolio has "
            f"σ ≈ {default_pct:.0f}%. Lower σ = smaller year-to-year "
            f"swings; higher σ = bigger swings (in both directions)."
        )
        return AssumptionField(
            value=sigma_value,
            source="sigma_calibrator",
            rationale_md=rationale,
        )
    except Exception:  # pragma: no cover - defensive degradation
        return AssumptionField(
            value=DEFAULT_SIGMA_ANNUAL,
            source="default",
            rationale_md=RATIONALE_SIGMA_DEFAULT,
        )


def _resolve_tax_rate(goals: dict) -> AssumptionField:
    """Resolve effective tax rate.

    Wave 8 v2 polish: rationale text now explains the Israeli
    age-aware tax model the Monte Carlo simulation ACTUALLY uses
    (25% pre-60 → 15% 60-67 → 12% 67+) rather than telling the user
    to "adjust if your effective rate is different". The flat
    `tax_rate` value here is the headline single-number rate used in
    the deterministic projection; the MC engine internally bands
    the rate by age via apply_age_aware_tax=True.
    """
    raw = goals.get("tax_rate_pct")
    val = _coerce_float(raw)
    if val is not None and 0.0 <= val <= 0.5:
        return AssumptionField(
            value=val,
            source="goals_yaml",
            rationale_md=(
                f"From your goals_yaml `tax_rate_pct`: {val * 100:.1f}%. "
                "This is the headline single-number rate used in the "
                "deterministic projection; the Monte Carlo engine bands "
                "the effective rate by age (25% pre-60 → 15% 60-67 → "
                "12% 67+ — Israeli CGT + age-aware pension tax breaks)."
            ),
        )
    return AssumptionField(
        value=DEFAULT_TAX_RATE,
        source="default",
        rationale_md=(
            "Israeli capital-gains marginal rate (25%) is the headline "
            "default for the deterministic projection. The Monte Carlo "
            "engine actually applies an age-banded effective rate "
            "internally — 25% pre-60, 15% during the lump-pension "
            "window (60-67), 12% from age 67 onward — so you'd see "
            "different effective rates in MC results vs the slider's "
            "single number. Set `tax_rate_pct` in goals_yaml if your "
            "effective rate is meaningfully different."
        ),
    )


def _resolve_retirement_age(goals: dict) -> AssumptionField:
    raw = goals.get("retirement_target_age")
    val = _coerce_float(raw)
    if val is not None and 30.0 <= val <= 80.0:
        return AssumptionField(
            value=val,
            source="goals_yaml",
            rationale_md=(
                f"From your goals_yaml `retirement_target_age`: age {val:.0f}."
            ),
        )
    return AssumptionField(
        value=DEFAULT_RETIREMENT_AGE,
        source="default",
        rationale_md=RATIONALE_RETIREMENT_DEFAULT,
    )


def _resolve_lifestyle_drift(goals: dict) -> AssumptionField:
    raw = goals.get("lifestyle_drift_annual")
    val = _coerce_float(raw)
    if val is not None and 0.0 <= val <= 0.10:
        return AssumptionField(
            value=val,
            source="goals_yaml",
            rationale_md=(
                f"From your goals_yaml `lifestyle_drift_annual`: "
                f"{val * 100:.2f}% above CPI per year."
            ),
        )
    return AssumptionField(
        value=DEFAULT_LIFESTYLE_DRIFT_ANNUAL,
        source="default",
        rationale_md=RATIONALE_LIFESTYLE_DEFAULT,
    )


def get_default_assumptions(
    session: Session | None,
    user_id: str,
) -> DefaultAssumptionsResponse:
    """Top-level entry. Compute all six default-with-rationale fields."""
    goals = _load_goals_yaml(session, user_id)
    return DefaultAssumptionsResponse(
        mu_nominal_annual=_resolve_mu_nominal(session, user_id),
        sigma_annual=_resolve_sigma(session, user_id),
        tax_rate=_resolve_tax_rate(goals),
        inflation_annual=AssumptionField(
            value=DEFAULT_INFLATION_ANNUAL,
            source="default",
            rationale_md=RATIONALE_INFLATION,
        ),
        retirement_age=_resolve_retirement_age(goals),
        lifestyle_drift_annual=_resolve_lifestyle_drift(goals),
    )
