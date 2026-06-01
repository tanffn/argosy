"""Default cashflow-projection assumptions for the /plan recap (Wave 8 Piece C).

Provides six per-field defaults the recap's cashflow sliders should
pre-populate with, each carrying a ``source`` ("sigma_calibrator",
"goals_yaml", or "default") and a markdown rationale the UI can
render as a `▸ why?` tooltip.

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


AssumptionSource = Literal["sigma_calibrator", "goals_yaml", "default"]


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


def _resolve_sigma(
    session: Session | None, user_id: str
) -> AssumptionField:
    """Best-effort calibrate σ from the user's portfolio. Falls back
    to the unweighted default + a "calibration unavailable" rationale
    when the calibrator can't produce a portfolio-aware figure."""
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
        rationale = (
            f"Portfolio-weighted σ from your latest snapshot: "
            f"{sigma_value * 100:.1f}% (vs. the unweighted-equity default "
            f"{DEFAULT_SIGMA_ANNUAL * 100:.0f}%). "
            + (cal.sigma_annual.rationale or "")
        ).strip()
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
    raw = goals.get("tax_rate_pct")
    val = _coerce_float(raw)
    if val is not None and 0.0 <= val <= 0.5:
        return AssumptionField(
            value=val,
            source="goals_yaml",
            rationale_md=(
                f"From your goals_yaml `tax_rate_pct`: {val * 100:.1f}%."
            ),
        )
    return AssumptionField(
        value=DEFAULT_TAX_RATE,
        source="default",
        rationale_md=RATIONALE_TAX_DEFAULT,
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
        mu_nominal_annual=AssumptionField(
            value=DEFAULT_MU_NOMINAL_ANNUAL,
            source="default",
            rationale_md=RATIONALE_MU,
        ),
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
