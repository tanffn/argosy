"""Deterministic FI (financial-independence) methodology.

This is the single, auditable source of truth for the household's FI capital
target — the number the synthesizer used to FABRICATE (a round ₪21M) and the
number an earlier "derived" attempt got WRONG (₪6.15M = current T12 burn ÷ the
4.5% expected return). Both errors are fixed here:

1. **Spend basis = permanent-equivalent spend, not current T12 burn.** The FI
   target is sized on the spend the perpetuity must fund *forever* in real
   terms: the tracked baseline MINUS the finite mortgage, PLUS the amortized
   recurring life-event spend (car-replacement cadence, late-life healthcare
   ramp, home-upgrade cadence). One-shot finite liabilities (education
   contributions, the remaining mortgage balance) are NOT capitalized into the
   perpetuity — they are held in a separate liquidity reserve so the
   perpetuity isn't over-sized to temporary lump clusters.

2. **Yield = a defensible perpetual real safe-withdrawal rate, not the
   expected return.** The household's mandate is explicit
   (``goals_yaml.retirement_drawdown_style = capital_preservation_returns_only``,
   "no principal drawdown … 0% SWR on principal") over a 90+ year perpetuity.
   The classic 4% rule assumes a 30-year horizon with principal depletion and
   does not apply. After Israeli 25% real CGT drag, the central sustainable
   perpetual real withdrawal is taken as **3.0%** (band 2.4%–3.5%; 3.5% is the
   optimistic/conditional end). This is decoupled from the ~5% expected real
   *return* the trajectory grows at.

   fi_perpetuity = permanent_annual_spend / swr_real_pct

The component table is intentionally explicit: every line carries a value, a
source locator, and a confidence band. The low-confidence planning heuristics
(healthcare ramp, home upgrades) are documented deterministic parameters with
scenario tags rather than LLM judgment — the LLM consumes + explains + may
challenge them, but never invents the headline target.

Calibration cross-check: a prior household_budget phased-cashflow analysis
landed on a ~₪314k/yr permanent-equivalent spend (and a ~₪341k Phase-2 active-
retirement stress); the component sum here reproduces that class of number
from sourced inputs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import select

from argosy.state.models import UserContext

if TYPE_CHECKING:  # pragma: no cover — typing only
    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Documented planning parameters. Each is a deliberate, auditable assumption —
# NOT an LLM guess. Edit here (one place) to re-run the methodology.
# ---------------------------------------------------------------------------

# Central perpetual real safe-withdrawal rate (after-tax). Band documents the
# sensitivity an adversarial reviewer reconciles against.
SWR_REAL_CENTRAL_PCT = 0.030
SWR_REAL_BAND: tuple[float, float] = (0.024, 0.035)

# Expected real (after-inflation) portfolio return — used by the trajectory /
# earliest-feasible-age, NOT to size the target. Decoupled from the SWR.
RETURN_ASSUMPTION_REAL_PCT = 0.05

# Car replacement: a ~₪200k vehicle every 5 years → amortized annual reserve.
CAR_REPLACEMENT_NIS = 200_000.0
CAR_REPLACEMENT_EVERY_YEARS = 5.0

# Late-life healthcare ramp ABOVE the tracked baseline health spend — a
# blended permanent allowance (the ramp is back-loaded but the perpetuity
# must carry it). LOW confidence (heuristic).
HEALTHCARE_RAMP_PERMANENT_NIS = 15_000.0

# Home upgrade / major renovation cadence: ~₪150k every 10 years → amortized.
# LOW confidence (heuristic).
HOME_UPGRADE_NIS = 150_000.0
HOME_UPGRADE_EVERY_YEARS = 10.0

# Wedding-event parental contribution (distinct from the apartment gift) — a
# near-term finite liability buffer. LOW confidence.
WEDDING_BUFFER_NIS = 100_000.0


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FiComponent:
    """One line of the spend / liability derivation.

    ``annual_nis`` is the amortized permanent annual contribution (0 for a
    finite liability); ``reserve_nis`` is the finite-liability principal that
    goes to the separate reserve (0 for a permanent line). ``sign`` is +1 for
    additive and -1 for subtractive (e.g. mortgage runoff).
    """

    label: str
    kind: str  # "permanent" | "finite"
    annual_nis: float
    reserve_nis: float
    source: str
    confidence: str  # HIGH | MEDIUM | LOW


@dataclass(frozen=True)
class FiMethodology:
    """The full, auditable FI derivation."""

    baseline_annual_nis: float
    permanent_annual_spend_nis: float
    swr_real_pct: float
    swr_band: tuple[float, float]
    return_assumption_real_pct: float
    fi_perpetuity_nis: float           # THE headline FI target (perpetuity)
    finite_liability_reserve_nis: float
    fi_total_capital_nis: float        # perpetuity + reserve
    components: list[FiComponent] = field(default_factory=list)
    method: str = ""
    confidence: str = "MEDIUM"

    def perpetuity_at(self, swr: float) -> float:
        """FI perpetuity target at an alternate SWR (sensitivity band)."""
        if swr <= 0:
            return float("inf")
        return self.permanent_annual_spend_nis / swr


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_yaml(text: str | None) -> dict:
    if not text:
        return {}
    try:
        import yaml as _yaml

        data = _yaml.safe_load(text) or {}
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — defensive
        return {}


def _f(v: object) -> float | None:
    try:
        if v is None or isinstance(v, bool):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def compute_fi_target(
    session: "Session",
    *,
    user_id: str,
    spend_t12_nis: float | None = None,
    swr_real_pct: float | None = None,
) -> FiMethodology | None:
    """Compute the deterministic FI methodology for ``user_id``.

    Reads ``identity_yaml`` (tracked baseline spend + mortgage) and
    ``goals_yaml`` (education household contribution) and applies the
    documented life-event parameters. Returns ``None`` when the tracked
    baseline spend cannot be sourced (caller degrades to ``pending`` — never a
    fabricated constant).

    Args:
        spend_t12_nis: override the tracked baseline (e.g. the household_budget
            agent's monthly_burn × 12). When ``None`` the value is read from
            ``identity_yaml.monthly_expenses_total_nis × 12``.
        swr_real_pct: override the central perpetual SWR (defaults to
            :data:`SWR_REAL_CENTRAL_PCT`).
    """
    ctx = session.execute(
        select(UserContext).where(UserContext.user_id == user_id)
    ).scalar_one_or_none()
    identity = _load_yaml(getattr(ctx, "identity_yaml", None) if ctx else None)
    goals = _load_yaml(getattr(ctx, "goals_yaml", None) if ctx else None)

    # --- Baseline tracked spend (REAL, sourced). -----------------------------
    baseline = spend_t12_nis
    baseline_src = "household_budget.monthly_burn_nis * 12"
    if baseline is None:
        monthly = _f(identity.get("monthly_expenses_total_nis"))
        if monthly is not None and monthly > 0:
            baseline = monthly * 12.0
            baseline_src = "identity_yaml.monthly_expenses_total_nis * 12"
        else:
            annual = _f(identity.get("monthly_expenses_annual_nis"))
            if annual is not None and annual > 0:
                baseline = annual
                baseline_src = "identity_yaml.monthly_expenses_annual_nis"
    if baseline is None or baseline <= 0:
        log.warning("fi_methodology.no_baseline_spend user=%s", user_id)
        return None

    components: list[FiComponent] = []

    # --- Mortgage: finite, runs off. Subtract from the permanent baseline; -----
    # park the remaining principal in the reserve.
    breakdown = identity.get("monthly_expenses_breakdown") or {}
    mortgage_monthly = _f(breakdown.get("mortgage_nis")) if isinstance(breakdown, dict) else None
    mortgage_annual = (mortgage_monthly or 0.0) * 12.0
    mort_bal = identity.get("mortgage_balance") or {}
    il_mortgage_balance = _f(mort_bal.get("keret_1_nis")) if isinstance(mort_bal, dict) else None
    il_mortgage_balance = il_mortgage_balance or 0.0

    # --- Permanent components. ----------------------------------------------
    baseline_ex_mortgage = baseline - mortgage_annual
    components.append(FiComponent(
        label="Tracked baseline living (ex-mortgage)",
        kind="permanent",
        annual_nis=baseline_ex_mortgage,
        reserve_nis=0.0,
        source=f"{baseline_src} minus identity_yaml.monthly_expenses_breakdown.mortgage_nis*12",
        confidence="HIGH",
    ))
    car_amortized = CAR_REPLACEMENT_NIS / CAR_REPLACEMENT_EVERY_YEARS
    components.append(FiComponent(
        label=f"Car replacement (₪{CAR_REPLACEMENT_NIS:,.0f}/{CAR_REPLACEMENT_EVERY_YEARS:.0f}y, amortized)",
        kind="permanent",
        annual_nis=car_amortized,
        reserve_nis=0.0,
        source="planning_parameter:CAR_REPLACEMENT (user-stated cadence)",
        confidence="MEDIUM",
    ))
    components.append(FiComponent(
        label="Late-life healthcare ramp (permanent allowance above baseline)",
        kind="permanent",
        annual_nis=HEALTHCARE_RAMP_PERMANENT_NIS,
        reserve_nis=0.0,
        source="planning_parameter:HEALTHCARE_RAMP",
        confidence="LOW",
    ))
    home_amortized = HOME_UPGRADE_NIS / HOME_UPGRADE_EVERY_YEARS
    components.append(FiComponent(
        label=f"Home upgrades (₪{HOME_UPGRADE_NIS:,.0f}/{HOME_UPGRADE_EVERY_YEARS:.0f}y, amortized)",
        kind="permanent",
        annual_nis=home_amortized,
        reserve_nis=0.0,
        source="planning_parameter:HOME_UPGRADE",
        confidence="LOW",
    ))

    permanent_annual_spend = sum(c.annual_nis for c in components if c.kind == "permanent")

    # --- Finite liabilities → reserve bucket (NOT capitalized into the -------
    # perpetuity per the core-spend-plus-reserve design).
    edu = goals.get("education_funding_targets") or {}
    edu_contribution = None
    if isinstance(edu, dict):
        edu_contribution = _f(
            edu.get("combined_household_contribution_nis")
        ) or _f(edu.get("combined_household_liability_nis"))
    if edu_contribution:
        components.append(FiComponent(
            label="Education — household contribution (2 children)",
            kind="finite",
            annual_nis=0.0,
            reserve_nis=edu_contribution,
            source="goals_yaml.education_funding_targets.combined_household_contribution_nis",
            confidence="HIGH",
        ))
    if il_mortgage_balance > 0:
        components.append(FiComponent(
            label="Remaining mortgage balance (runs off)",
            kind="finite",
            annual_nis=0.0,
            reserve_nis=il_mortgage_balance,
            source="identity_yaml.mortgage_balance.keret_1_nis",
            confidence="HIGH",
        ))
    components.append(FiComponent(
        label="Wedding-event contribution buffer (near-term lumps)",
        kind="finite",
        annual_nis=0.0,
        reserve_nis=WEDDING_BUFFER_NIS,
        source="planning_parameter:WEDDING_BUFFER",
        confidence="LOW",
    ))

    reserve = sum(c.reserve_nis for c in components if c.kind == "finite")

    swr = swr_real_pct if (swr_real_pct and swr_real_pct > 0) else SWR_REAL_CENTRAL_PCT
    fi_perpetuity = permanent_annual_spend / swr
    fi_total = fi_perpetuity + reserve

    method = (
        f"FI perpetuity = permanent-equivalent spend ₪{permanent_annual_spend:,.0f}/yr "
        f"÷ {swr*100:.1f}% real after-tax perpetual SWR = ₪{fi_perpetuity:,.0f}; "
        f"finite liabilities (education, mortgage runoff, weddings) held in a "
        f"₪{reserve:,.0f} liquidity reserve, NOT capitalized into the perpetuity."
    )

    return FiMethodology(
        baseline_annual_nis=baseline,
        permanent_annual_spend_nis=permanent_annual_spend,
        swr_real_pct=swr,
        swr_band=SWR_REAL_BAND,
        return_assumption_real_pct=RETURN_ASSUMPTION_REAL_PCT,
        fi_perpetuity_nis=fi_perpetuity,
        finite_liability_reserve_nis=reserve,
        fi_total_capital_nis=fi_total,
        components=components,
        method=method,
        confidence="MEDIUM",
    )


__all__ = [
    "FiComponent",
    "FiMethodology",
    "compute_fi_target",
    "SWR_REAL_CENTRAL_PCT",
    "SWR_REAL_BAND",
    "RETURN_ASSUMPTION_REAL_PCT",
]
