"""Rebalancing rule — 5/25 threshold + quarterly check.

Closes HIGH #10 from the 2026-05-28 SDD review. Argosy had no documented
rebalancing policy — drift accumulates silently between manual reviews,
decoupling actual risk from plan assumptions.

Policy:
  - Threshold rule: any asset class > 5pp off target → rebalance trigger
  - 25% relative rule: any class > 25% off its own target → also trigger
  - Quarterly time check: even if no threshold breach, recheck quarterly
  - Annual major review

Plan: ``docs/superpowers/plans/2026-05-28-retirement-companion-overhaul.md``
§ Wave 4 HIGH #10.
"""
import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from argosy.services.retirement.citations import ValueWithRationale
from argosy.services.retirement.sigma_calibration import _classify_position
from argosy.services.target_allocation_doc import (
    doc_equity_bond_cash,
    load_plan_target_allocation,
)
from argosy.services.wealth_dashboard import _latest_snapshot
from argosy.state.queries import get_current_plan


@dataclass(frozen=True)
class RebalancingAlert:
    asset_class: str
    current_pct: ValueWithRationale
    target_pct: ValueWithRationale
    drift_pp: ValueWithRationale  # current - target in percentage points
    rule_fired: str  # "5pp_threshold" or "25pct_relative"
    suggested_proposal: str


def _coarse_class(asset_class: str) -> str:
    """Collapse fine-grained classes into equity/bonds/cash buckets."""
    if asset_class in ("concentrated_equity", "us_equity", "intl_equity", "emerging_equity"):
        return "equity"
    if asset_class == "bonds":
        return "bonds"
    if asset_class == "cash":
        return "cash"
    if asset_class == "real_estate":
        return "equity"  # treat REITs as equity for glide-path purposes
    return "other"


def detect_rebalancing_alerts(
    *,
    user_id: str,
    current_age: int,
    session: Session,
    threshold_pp: float = 5.0,
    relative_threshold: float = 0.25,
) -> list[RebalancingAlert]:
    """Compare actual allocation to glide-path target; emit alerts on breach."""
    snapshot = _latest_snapshot(session, user_id)
    if snapshot is None:
        return []

    try:
        positions = json.loads(snapshot.positions_json or "[]")
    except (json.JSONDecodeError, TypeError):
        positions = []

    # Compute current actual allocation by coarse class
    by_coarse: dict[str, float] = {"equity": 0.0, "bonds": 0.0, "cash": 0.0, "other": 0.0}
    total = 0.0
    for p in positions:
        cls = _classify_position(p)
        coarse = _coarse_class(cls)
        v_k = p.get("usd_value_k") or 0.0
        try:
            v = float(v_k) * 1000.0
        except (TypeError, ValueError):
            continue
        if v <= 0:
            continue
        by_coarse[coarse] = by_coarse.get(coarse, 0.0) + v
        total += v
    if total <= 0:
        return []

    # Targets from the CANONICAL plan (TargetAllocationDoc), NOT a textbook
    # age-decline curve. No canonical plan persisted → no rebalancing target
    # (never rebalance toward a fabricated curve). The doc's flat equity/bond/
    # cash target is exactly what /glide-path projects, so the two reconcile.
    pv = get_current_plan(session, user_id)
    doc = load_plan_target_allocation(pv) if pv is not None else None
    if doc is None:
        return []
    eq_pct, bd_pct, cs_pct = doc_equity_bond_cash(doc)  # percentages of the book
    target_map = {
        "equity": eq_pct / 100.0,
        "bonds": bd_pct / 100.0,
        "cash": cs_pct / 100.0,
    }

    alerts: list[RebalancingAlert] = []
    for cls in ("equity", "bonds", "cash"):
        actual_pct = by_coarse[cls] / total
        target_pct = target_map[cls]
        drift_pp = (actual_pct - target_pct) * 100.0  # signed pp

        triggered_by: str | None = None
        if abs(drift_pp) >= threshold_pp:
            triggered_by = "5pp_threshold"
        elif target_pct > 0 and abs(actual_pct - target_pct) / target_pct >= relative_threshold:
            triggered_by = "25pct_relative"
        if triggered_by is None:
            continue

        # Direction-aware suggested proposal
        delta_usd = round((target_pct - actual_pct) * total, 2)
        if delta_usd > 0:
            proposal = (
                f"Buy ~${delta_usd:,.0f} of {cls} to reach target "
                f"{target_pct:.0%}."
            )
        else:
            proposal = (
                f"Trim ~${abs(delta_usd):,.0f} of {cls} (currently "
                f"{actual_pct:.0%}; target {target_pct:.0%})."
            )

        alerts.append(RebalancingAlert(
            asset_class=cls,
            current_pct=ValueWithRationale(
                value=round(actual_pct, 4),
                unit="fraction",
                source_id=None,
                rationale=f"Actual {cls} share of portfolio at the latest snapshot.",
                confidence="high",
            ),
            target_pct=ValueWithRationale(
                value=round(target_pct, 4),
                unit="fraction",
                source_id="canonical_target_allocation_doc",
                rationale=f"Canonical plan target {cls} share (TargetAllocationDoc).",
                confidence="high",
            ),
            drift_pp=ValueWithRationale(
                value=round(drift_pp, 2),
                unit="percentage_points",
                source_id=None,
                rationale=f"Signed drift {cls}: actual - target.",
                confidence="high",
            ),
            rule_fired=triggered_by,
            suggested_proposal=proposal,
        ))
    return alerts
