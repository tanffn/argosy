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
    # Lot-level Section-102 eligibility from the latest ingested tax-simulation report —
    # turns the deconcentration horizon from an ASSUMPTION into data (how many shares are
    # capital-track-eligible NOW). Best-effort; absent => the policy horizon stands.
    try:
        from argosy.services.tax_simulation_ingest import eligible_shares
        elig = eligible_shares(session, user_id)
        if elig is not None:
            facts["nvda_eligible_now_sh"] = int(elig)
            brk = eligible_shares(session, user_id, eligible=False)
            facts["nvda_breaking_sh"] = int(brk or 0)
    except Exception:  # noqa: BLE001
        pass
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
        line = (
            f"- NVDA deconcentration: reduce to the {facts['nvda_target_w']:.0%} IPS "
            f"target = retain ~{facts['nvda_target_sh']:,} shares, SELL ~"
            f"{facts['nvda_sell_sh']:,} shares (position breaches the risk cap by "
            f"{facts['nvda_cap_breach_x']}x). "
        )
        elig = facts.get("nvda_eligible_now_sh")
        if elig is not None:
            brk = facts.get("nvda_breaking_sh", 0)
            if elig >= facts["nvda_sell_sh"]:
                line += (
                    f"Horizon: NOW. {elig:,} shares are ALREADY Section-102 capital-track "
                    f"eligible (~25%), which covers the entire ~{facts['nvda_sell_sh']:,}-"
                    f"share sale — execute it now at capital rates; do NOT wait for 2027. "
                    f"Only ~{brk:,} 'Breaking' shares (recent ESPP/late grants) should "
                    f"season first to avoid ~62% ordinary tax."
                )
            else:
                line += (
                    f"Horizon: sell the {elig:,} capital-track-eligible shares NOW (~25%); "
                    f"the remaining ~{facts['nvda_sell_sh'] - elig:,} must wait for lots to "
                    f"season past their Section-102 clock (selling sooner = ~62% ordinary)."
                )
        else:
            line += (
                "Horizon: front-load as lots clear their Section-102 capital-track clock; "
                "ingest the Schwab tax-lot report to make this lot-exact."
            )
        lines.append(line)
    if "fi_margin_liquid_nis" in facts:
        m = facts["fi_margin_liquid_nis"]
        lines.append(
            f"- FI sufficiency (HONEST liquid basis): margin = {m:,.0f} NIS -> FI "
            f"{'MET' if m >= 0 else 'NOT met'}. Use the LIQUID basis, never the investable "
            "basis (which overstates FI). Do NOT claim FI reached if the margin is negative."
        )
    return "\n".join(lines)
