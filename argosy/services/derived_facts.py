# argosy/services/derived_facts.py
"""LOCKED DERIVED FACTS for the synthesizer.

Derive the load-bearing numbers (NVDA deconcentration, FI-liquid margin) from the
resolver + the latest holdings snapshot, and render them as a guidance block the
synthesizer MUST use — so it derives instead of inheriting a cadence/target from the
baseline doc (the ``3,000 sh/yr`` class of error). Best-effort: returns None if it cannot
derive, so the injection degrades to a no-op (the fail-closed promote gate is the separate
backstop).
"""
from __future__ import annotations

import json

import sqlalchemy as sa

from argosy.services.plan_derivation import (
    derive_fi_margin_liquid, derive_nvda_deconcentration,
)

IPS_NVDA_TARGET_W = 0.12  # IPS sleeve target (a stated policy constraint, not inherited)


def _resolved_value(resolved, key):
    try:
        v = resolved.get(key)
    except Exception:  # noqa: BLE001
        return None
    return getattr(v, "value", None) if v is not None else None


def _latest_nvda(session, user_id: str):
    try:
        raw = session.execute(
            sa.text(
                "select positions_json from portfolio_snapshots where user_id=:u "
                "order by snapshot_date desc, id desc limit 1"
            ),
            {"u": user_id},
        ).scalar()
        positions = json.loads(raw) if raw else []
        if isinstance(positions, dict):
            positions = positions.get("positions", [])
        for p in positions or []:
            if isinstance(p, dict) and str(p.get("symbol", "")).upper() == "NVDA":
                return float(p["shares"]), float(p["current_price"])
    except Exception:  # noqa: BLE001
        pass
    return None, None


def build_derived_facts(session, *, user_id: str, decision_run_id=None) -> dict | None:
    """Compute the locked derived facts; None if inputs are unavailable."""
    from argosy.services.plan_numeric_resolver import resolve_plan_numbers

    try:
        resolved = resolve_plan_numbers(
            session, user_id=user_id, decision_run_id=decision_run_id
        )
    except Exception:  # noqa: BLE001 — fail-soft; injection no-ops
        return None

    nvda_w = _resolved_value(resolved, "concentration.nvda_current_pct")
    cap = _resolved_value(resolved, "concentration.nvda_cap_pct")
    liquid = _resolved_value(resolved, "portfolio.liquid_net_worth_nis")
    fi_total = _resolved_value(resolved, "retirement.fi_total_capital_nis")
    nvda_sh, nvda_px = _latest_nvda(session, user_id)

    facts: dict = {}
    if None not in (nvda_w, cap, nvda_sh, nvda_px) and nvda_w > 0:
        dec = derive_nvda_deconcentration(
            nvda_sh=int(nvda_sh), nvda_px_usd=nvda_px, nvda_weight=nvda_w,
            target_w=IPS_NVDA_TARGET_W, cap=cap,
        )
        facts["nvda_target_w"] = IPS_NVDA_TARGET_W
        facts["nvda_target_sh"] = dec["nvda_target_sh"].value
        facts["nvda_sell_sh"] = dec["nvda_sell_sh"].value
        facts["nvda_cap_breach_x"] = dec["nvda_cap_breach_x"].value
    if None not in (liquid, fi_total):
        facts["fi_margin_liquid_nis"] = derive_fi_margin_liquid(
            liquid_nw_nis=liquid, fi_total_capital_nis=fi_total,
        ).value
    return facts or None


def render_derived_facts_guidance(facts: dict | None) -> str:
    """Render the locked-facts block prepended to synthesizer guidance."""
    if not facts:
        return ""
    lines = [
        "LOCKED DERIVED FACTS — use these EXACT numbers as canonical. Do NOT inherit or "
        "restate any NVDA sale cadence/target from the baseline plan doc or prior drafts "
        "(a '3,000 sh/yr' figure is FORBIDDEN — it is past behavior, not a derived "
        "target). Derive all forward guidance from these:",
    ]
    if "nvda_target_sh" in facts:
        lines.append(
            f"- NVDA deconcentration: reduce to the {facts['nvda_target_w']:.0%} IPS "
            f"target = retain ~{facts['nvda_target_sh']:,} shares, SELL ~"
            f"{facts['nvda_sell_sh']:,} shares (position breaches the risk cap by "
            f"{facts['nvda_cap_breach_x']}x). Horizon: 2026 H2 capital-track-eligible "
            "lots only, then front-load once the Section-102 capital-track window opens "
            "2027-01-01, reaching target by ~mid-2027 (no tax reason to stretch to 2028)."
        )
    if "fi_margin_liquid_nis" in facts:
        m = facts["fi_margin_liquid_nis"]
        lines.append(
            f"- FI sufficiency (HONEST liquid basis): margin = {m:,.0f} NIS -> FI "
            f"{'MET' if m >= 0 else 'NOT met'}. Use the LIQUID basis, never the investable "
            "basis (which overstates FI). Do NOT claim FI reached if the margin is negative."
        )
    return "\n".join(lines)
