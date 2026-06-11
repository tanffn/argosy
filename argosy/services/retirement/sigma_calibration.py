"""Auto-calibrate portfolio sigma from holdings concentration.

Closes HIGH #7 from the 2026-05-28 SDD review. The prior projection used
a hardcoded σ = 0.18 (S&P 500 diversified) for every user. For Ariel's
60%+ NVDA portfolio this silently understates tail risk by ~40-50%.

Approach: weighted average of per-asset volatilities. Asset-class
volatilities are calibrated to historical post-1970 data:

  - NVDA / single-stock concentrated equity:  σ ≈ 0.45
  - Diversified US equity (S&P 500):          σ ≈ 0.18
  - International developed equity:           σ ≈ 0.20
  - Emerging markets equity:                  σ ≈ 0.25
  - Bonds (investment-grade):                 σ ≈ 0.06
  - Cash / HYSA / money-market:               σ ≈ 0.02

Weighting: portfolio_fraction × class_sigma summed in quadrature for
independent assets, or as a simple weighted average when correlation is
high (NVDA + NVDA-correlated tech). We use a simple weighted average as
a conservative default — it slightly overstates sigma for diversified
holdings, which biases toward "better safe than sorry".

Plan: ``docs/superpowers/plans/2026-05-28-retirement-companion-overhaul.md``
§ Wave 3 HIGH #7.
"""
import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from argosy.services.retirement.citations import ValueWithRationale
from argosy.services.wealth_dashboard import _latest_snapshot


# Asset-class sigma defaults (historical post-1970 annualized).
_SIGMA_BY_CLASS: dict[str, float] = {
    "concentrated_equity": 0.45,  # single-stock heavy (NVDA, TSLA, etc.)
    "us_equity": 0.18,             # diversified US (S&P 500)
    "low_vol_equity": 0.13,        # min-vol / quality-defensive equity (USMV-like):
                                   # ~0.11-0.13 realized vs ~0.15-0.18 for the cap-
                                   # weighted market — a real equity sleeve, NOT the
                                   # 0.06 IG-bond floor it was being mis-mapped to.
    "intl_equity": 0.20,           # developed international (VEA, etc.)
    "emerging_equity": 0.25,       # emerging markets
    "bonds": 0.06,                 # investment-grade bonds (SGOV, BND)
    "cash": 0.02,                  # cash / HYSA / MMF
    "real_estate": 0.15,           # REITs / real estate funds
    "other": 0.20,                 # unknown class — assume diversified equity
}


# Tickers we treat as "concentrated single-stock equity" even when they
# would naively classify as "us_equity" by ETF heuristics.
_CONCENTRATED_TICKERS = {"NVDA", "TSLA", "META", "GOOG", "GOOGL", "AAPL", "MSFT"}


@dataclass(frozen=True)
class SigmaCalibration:
    sigma_annual: ValueWithRationale
    breakdown: list[dict]  # per-class contribution: {class, weight, sigma, contribution}
    portfolio_total_usd: ValueWithRationale


def _classify_position(p: dict) -> str:
    """Return the asset class for a portfolio position."""
    symbol = (p.get("symbol") or "").strip().upper()
    asset_type = (p.get("asset_type") or "").lower()
    details = (p.get("details") or "").lower()

    if "cash" in asset_type:
        return "cash"
    if "real estate" in asset_type or "real estate" in details:
        return "real_estate"
    if "treasury" in details or symbol in (
        "SGOV", "BND", "AGG", "TLT", "SHY",
        "IB01", "IBTA", "CBU3", "ERNS",  # UCITS $-Treasury twins (domicile-aware plan)
    ):
        return "bonds"
    if symbol in _CONCENTRATED_TICKERS:
        return "concentrated_equity"
    # ETFs default to diversified US equity unless detail tags say otherwise
    if "etf" in asset_type:
        if any(t in details for t in ("emerging", "emrg", "vwo")):
            return "emerging_equity"
        if any(t in details for t in ("international", "intl", "vea", "veu")):
            return "intl_equity"
        return "us_equity"
    if "nvidia" in asset_type or asset_type == "nvidia":
        return "concentrated_equity"
    return "other"


def calibrate_sigma_from_holdings(
    *,
    user_id: str,
    session: Session,
) -> SigmaCalibration:
    """Compute a portfolio-weighted sigma from the latest snapshot.

    Returns a SigmaCalibration with the headline sigma + per-class
    breakdown for the Sensitivity drilldown.
    """
    snapshot = _latest_snapshot(session, user_id)
    if snapshot is None:
        return SigmaCalibration(
            sigma_annual=ValueWithRationale(
                value=0.18,
                unit="fraction",
                source_id="damodaran_implied_erp_2026",
                rationale="No portfolio snapshot available; defaulting to diversified-equity sigma 0.18.",
                confidence="low",
            ),
            breakdown=[],
            portfolio_total_usd=ValueWithRationale(
                value=0.0,
                unit="USD",
                source_id=None,
                rationale="No portfolio snapshot.",
                confidence="low",
            ),
        )

    try:
        positions = json.loads(snapshot.positions_json or "[]")
    except (json.JSONDecodeError, TypeError):
        positions = []

    # Aggregate weights by class
    class_value: dict[str, float] = {}
    total = 0.0
    for p in positions:
        v_k = p.get("usd_value_k") or 0.0
        try:
            v = float(v_k) * 1000.0
        except (TypeError, ValueError):
            continue
        if v <= 0:
            continue
        cls = _classify_position(p)
        class_value[cls] = class_value.get(cls, 0.0) + v
        total += v

    if total <= 0:
        sigma = 0.18
        breakdown: list[dict] = []
    else:
        # Weighted average sigma (conservative bias; understates diversification benefit)
        sigma = 0.0
        breakdown = []
        for cls, val in sorted(class_value.items(), key=lambda kv: -kv[1]):
            weight = val / total
            cls_sigma = _SIGMA_BY_CLASS.get(cls, 0.20)
            contribution = weight * cls_sigma
            sigma += contribution
            breakdown.append({
                "asset_class": cls,
                "weight_pct": round(weight * 100, 2),
                "sigma": cls_sigma,
                "contribution": round(contribution, 4),
                "usd_value": round(val, 2),
            })

    concentrated_pct = round(
        100.0 * class_value.get("concentrated_equity", 0.0) / max(total, 1.0), 1,
    )

    return SigmaCalibration(
        sigma_annual=ValueWithRationale(
            value=round(sigma, 4),
            unit="fraction",
            source_id=None,
            rationale=(
                f"Weighted average of asset-class sigmas across the user's "
                f"portfolio. Concentrated-equity exposure: {concentrated_pct}%. "
                f"At {concentrated_pct}% single-stock concentration, the diversified "
                f"S&P-500 σ=0.18 would silently understate tail risk; this "
                f"calibration lifts σ to {sigma:.3f}."
            ),
            alternatives_considered=[
                "Diversified default σ=0.18 (S&P 500 historical) — understates "
                "tail risk for concentrated portfolios.",
                "Single-stock σ≈0.45 (NVDA-only) — overstates risk if you "
                "hold meaningful bonds + cash.",
            ],
            confidence="medium",
        ),
        breakdown=breakdown,
        portfolio_total_usd=ValueWithRationale(
            value=round(total, 2),
            unit="USD",
            source_id=None,
            rationale="Sum of positions_json usd_value_k × 1000 in the latest snapshot.",
            confidence="high",
        ),
    )
