"""Derived retirement-execution inputs — the single source of truth the
``/retirement`` page consumes instead of hardcoded props.

Doctrine (auto-memory ``feedback_output_trust_doctrine``): /retirement TRACKS
PLAN EXECUTION — every figure it shows must derive from the plan + the shared
resolver + the portfolio/identity state, never a hardcoded magic number. This
service assembles all of them, each as a :class:`DerivedField` carrying value
+ source + confidence + status. Where Argosy genuinely lacks the datum, the
field is ``status="pending"`` (the UI shows "needs intake"), NEVER a guess.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import desc, select

from argosy.logging import get_logger
from argosy.state.models import PlanVersion, UserContext

log = get_logger(__name__)


@dataclass(frozen=True)
class DerivedField:
    value: float | int | bool | None
    unit: str           # nis | pct | age | count | bool | fx
    source: str         # locator the value traces to
    confidence: str     # HIGH | MEDIUM | LOW
    status: str = "resolved"  # resolved | pending

    @classmethod
    def pending(cls, unit: str, source: str) -> "DerivedField":
        return cls(value=None, unit=unit, source=source, confidence="LOW", status="pending")


def _yaml(text: str | None) -> dict:
    if not text:
        return {}
    try:
        import yaml
        d = yaml.safe_load(text) or {}
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _f(v: Any) -> float | None:
    try:
        if v is None or isinstance(v, bool):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _age_from_dob(dob_str: str | None, today: date) -> float | None:
    if not dob_str:
        return None
    try:
        dob = datetime.fromisoformat(str(dob_str)).date()
    except (ValueError, TypeError):
        try:
            dob = datetime.strptime(str(dob_str), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None
    return round((today - dob).days / 365.25, 2)


def compute_derived_inputs(session, *, user_id: str, today: date | None = None) -> dict[str, Any]:
    """Assemble every /retirement input from derived sources.

    Returns ``{field_name: DerivedField-as-dict}`` plus a top-level
    ``decision_run_id`` (the current plan's run the figures track).
    """
    today = today or datetime.now(timezone.utc).date()

    # --- The plan this execution view tracks: the current plan_version. -----
    pv = session.execute(
        select(PlanVersion)
        .where(PlanVersion.user_id == user_id, PlanVersion.role == "current")
        .order_by(desc(PlanVersion.id)).limit(1)
    ).scalar_one_or_none()
    if pv is None:  # fall back to the freshest draft so a pre-accept plan still shows
        pv = session.execute(
            select(PlanVersion)
            .where(PlanVersion.user_id == user_id, PlanVersion.role == "draft")
            .order_by(desc(PlanVersion.id)).limit(1)
        ).scalar_one_or_none()
    drun = getattr(pv, "decision_run_id", None) if pv else None

    # --- Resolver manifest (FI numbers, net worth, nvda, savings, age). -----
    resolved = None
    if drun is not None:
        try:
            from argosy.services.plan_numeric_resolver import resolve_plan_numbers
            resolved = resolve_plan_numbers(session, user_id=user_id, decision_run_id=int(drun))
        except Exception as exc:  # noqa: BLE001
            log.warning("derived_inputs.resolver_failed", error=str(exc))

    def rv(key: str):
        return resolved.get(key) if resolved is not None else None

    def fld_from_rv(key: str, unit: str) -> DerivedField:
        v = rv(key)
        if v is None or v.status != "resolved" or v.value is None:
            loc = v.source_locator if v is not None else f"{key} (no plan run)"
            return DerivedField.pending(unit, loc)
        return DerivedField(float(v.value), unit, v.source_locator, v.confidence or "MEDIUM")

    # --- fi_methodology (spend, μ, SWR band). -------------------------------
    try:
        from argosy.services.fi_methodology import compute_fi_target
        m = compute_fi_target(session, user_id=user_id)
    except Exception:  # noqa: BLE001
        m = None

    # --- identity / goals YAML. ---------------------------------------------
    ctx = session.execute(
        select(UserContext).where(UserContext.user_id == user_id)
    ).scalar_one_or_none()
    idy = _yaml(getattr(ctx, "identity_yaml", None) if ctx else None)
    pa = idy.get("pensions_ariel") or {}
    pens = idy.get("pensions") or {}  # keren_hishtalmut / kupat_gemel / kupat_pensia blocks
    mort = idy.get("mortgage_balance") or {}

    out: dict[str, Any] = {"decision_run_id": int(drun) if drun is not None else None}

    def put(name: str, fld: DerivedField) -> None:
        out[name] = asdict(fld)

    # Ages — derive from DOB; never the stale stored integer.
    age = _age_from_dob(idy.get("user_date_of_birth"), today)
    put("current_age", DerivedField(age, "age", "identity_yaml.user_date_of_birth", "HIGH")
        if age is not None else DerivedField.pending("age", "identity_yaml.user_date_of_birth"))
    # Retirement age — the CANONICAL dual-track earliest-safe age (the
    # corrected drawdown age from the sigma-glide + NVDA-CGT + PV-reserve Monte
    # Carlo), NOT the stale withdrawal_sequencer fi_age. Lazy import (the
    # retirement engine ↔ wealth_dashboard circular-import trap) + best-effort:
    # any failure (thin data, no FI basis) falls back to the resolved
    # retirement.fi_age so nothing breaks. Never fabricated.
    fi_age_fld = fld_from_rv("retirement.fi_age", "age")
    canon_fld: DerivedField | None = None
    try:
        from argosy.services.retirement.retirement_plan import (
            canonical_feasible_dual_track,
        )
        canon = canonical_feasible_dual_track(session=session, user_id=user_id)
        canon_age = getattr(canon, "earliest_feasible_age", None)
        if canon_age is not None:
            conf = "HIGH" if canon.p_solvent_at_age is not None else "MEDIUM"
            canon_fld = DerivedField(
                float(canon_age), "age",
                "retirement_plan.canonical_feasible_dual_track.earliest_feasible_age",
                conf,
            )
    except Exception as exc:  # noqa: BLE001 — best-effort; fall back to fi_age
        log.warning("derived_inputs.canonical_age_failed", error=str(exc))
    put("retirement_age", canon_fld if canon_fld is not None else fi_age_fld)

    # FX spot — the current Bank-of-Israel rate (cache), the FX source of truth
    # (codex FX review). Falls back to the snapshot/identity fx only if BOI is
    # uncached. Kills both the hardcoded 3.4 the StochasticFxCard used and the
    # erroneous snapshot 2.94.
    fx = None
    fx_src = "boi USD/NIS current (FxRate cache)"
    try:
        from argosy.services.fx import cache as _fxcache
        fx = float(_fxcache.find_walkback(session, today, "USD", max_days=10))
    except Exception:  # noqa: BLE001
        try:
            from argosy.services.cashflow_projection import _latest_snapshot, _resolve_fx_usd_nis
            fx_val, _ = _resolve_fx_usd_nis(snapshot=_latest_snapshot(session, user_id), user_ctx=idy)
            fx = float(fx_val) if fx_val else None
            fx_src = "portfolio_snapshot fx (BOI uncached)"
        except Exception:  # noqa: BLE001
            fx = None
    put("fx_usd_nis", DerivedField(fx, "fx", fx_src, "HIGH")
        if fx else DerivedField.pending("fx", fx_src))

    # Mekadem (annuity divisor) — the documented planning default until a
    # fund-specific value is provided. An auditable default, not a bare magic
    # number in the UI.
    from argosy.services.cashflow_projection import DEFAULT_MEKADEM
    put("mekadem_typical", DerivedField(
        float(DEFAULT_MEKADEM), "count", "planning_parameter:DEFAULT_MEKADEM", "MEDIUM"))

    # Bituach Leumi insured-years — the standard convention (full-time work
    # from ~age 22), the same the BL card uses. Derived from age, not hardcoded.
    bl_years = max(0, int(round(age)) - 22) if age is not None else None
    put("bl_contribution_history_years", DerivedField(
        bl_years, "count", "convention: insured since age 22 (current_age − 22)", "MEDIUM")
        if bl_years is not None else DerivedField.pending("count", "needs current_age"))

    # Spend / income (monthly).
    monthly_need = (m.permanent_annual_spend_nis / 12.0) if m else None
    put("monthly_need_nis", DerivedField(monthly_need, "nis",
        "fi_methodology.permanent_annual_spend_nis / 12", "MEDIUM")
        if monthly_need else DerivedField.pending("nis", "fi_methodology.permanent_annual_spend_nis"))
    burn = _f(idy.get("monthly_expenses_total_nis"))
    put("monthly_burn_nis", DerivedField(burn, "nis", "identity_yaml.monthly_expenses_total_nis", "HIGH")
        if burn else DerivedField.pending("nis", "identity_yaml.monthly_expenses_total_nis"))
    u_net = _f(idy.get("employment_user_net_monthly_nis"))
    sp_net = _f(idy.get("spouse_net_monthly_nis"))
    if u_net is not None:
        income = u_net + (sp_net or 0.0)
        src = "identity_yaml.employment_user_net_monthly_nis" + (" + spouse_net_monthly_nis" if sp_net else "")
        put("monthly_income_nis", DerivedField(income, "nis", src, "MEDIUM" if sp_net else "HIGH"))
    else:
        put("monthly_income_nis", DerivedField.pending("nis", "identity_yaml.employment_user_net_monthly_nis"))

    # Account balances (tax-advantaged buckets + taxable brokerage).
    def bal(name: str, value: float | None, src: str, conf: str = "MEDIUM") -> None:
        put(name, DerivedField(value, "nis", src, conf) if value else DerivedField.pending("nis", src))

    bal("hishtalmut_balance_nis",
        _f(pa.get("keren_hishtalmut_nis")) or _f((pens.get("keren_hishtalmut") or {}).get("balance_nis")),
        "identity_yaml.pensions_ariel.keren_hishtalmut_nis", "HIGH")
    bal("kupat_gemel_balance_nis", _f((pens.get("kupat_gemel") or {}).get("balance_nis")),
        "identity_yaml.pensions.kupat_gemel.balance_nis", "HIGH")
    pension = _f(pa.get("pension_nis")) or _f((pens.get("kupat_pensia") or {}).get("balance_nis"))
    bal("pension_balance_nis", pension, "identity_yaml.pensions_ariel.pension_nis", "HIGH")
    bal("executive_insurance_nis", _f(pa.get("executive_insurance_nis")), "identity_yaml.pensions_ariel.executive_insurance_nis", "HIGH")

    # Taxable brokerage ≈ net worth (the tradeable portfolio), from the resolver.
    put("taxable_balance_nis", fld_from_rv("portfolio.net_worth_nis", "nis"))
    put("net_worth_nis", fld_from_rv("portfolio.net_worth_nis", "nis"))

    # Mortgage (real estate card).
    bal("mortgage_balance_nis", _f(mort.get("keret_1_nis")), "identity_yaml.mortgage_balance.keret_1_nis", "HIGH")
    rv_res = _f(idy.get("primary_residence_value_nis"))
    put("residence_value_nis", DerivedField(rv_res, "nis", "identity_yaml.primary_residence_value_nis", "MEDIUM")
        if rv_res else DerivedField.pending("nis", "identity_yaml.primary_residence_value_nis (needs intake)"))

    # Family.
    dep = idy.get("dependents_count")
    put("dependents_count", DerivedField(int(dep), "count", "identity_yaml.dependents_count", "HIGH")
        if isinstance(dep, int) else DerivedField.pending("count", "identity_yaml.dependents_count"))
    # children is a list of {age: N}; dependents_ages may also carry them.
    children = idy.get("children")
    ages: list[float] = []
    if isinstance(children, list):
        for it in children:
            a = _f(it.get("age")) if isinstance(it, dict) else None
            if a is not None:
                ages.append(a)
    if not ages and isinstance(idy.get("dependents_ages"), list):
        ages = [a for a in (_f(x) for x in idy["dependents_ages"]) if a is not None]
    kids_under_18 = any(a < 18 for a in ages) if ages else None
    put("has_kids_under_18", DerivedField(kids_under_18, "bool", "identity_yaml.children[*].age", "HIGH")
        if kids_under_18 is not None else DerivedField.pending("bool", "identity_yaml.children"))

    # FI headline numbers + market assumptions (for the cards that need them).
    put("fi_target_nis", fld_from_rv("retirement.fi_target_nis", "nis"))
    put("fi_total_capital_nis", fld_from_rv("retirement.fi_total_capital_nis", "nis"))
    put("liquidity_reserve_nis", fld_from_rv("retirement.liquidity_reserve_nis", "nis"))

    # FIRE bridge: from retirement to the first pension unlock (age 60), spend
    # is funded entirely from liquid assets. Requirement = bridge years ×
    # permanent-equivalent annual spend — NOT the lower T12 burn the plan doc
    # previously used (codex residual). Derived; pending until the retirement
    # age resolves (never fabricated).
    from argosy.services.cashflow_projection import LUMP_PENSION_AGE
    ret_v = rv("retirement.fi_age")
    ret_age = (
        float(ret_v.value)
        if (ret_v is not None and ret_v.status == "resolved" and ret_v.value is not None)
        else None
    )
    perm_annual = (m.permanent_annual_spend_nis if m else None)
    bridge_src = (
        f"({LUMP_PENSION_AGE} − retirement.fi_age) yrs × "
        "fi_methodology.permanent_annual_spend_nis"
    )
    if ret_age is not None and perm_annual:
        bridge_years = max(0.0, float(LUMP_PENSION_AGE) - ret_age)
        put("fire_bridge_requirement_nis", DerivedField(
            bridge_years * float(perm_annual), "nis", bridge_src, "MEDIUM"))
    else:
        put("fire_bridge_requirement_nis", DerivedField.pending("nis", bridge_src))
    put("required_real_yield_pct", fld_from_rv("retirement.required_real_yield_pct", "pct"))
    put("expected_real_return_pct", fld_from_rv("retirement.return_assumption_pct", "pct"))
    put("nvda_cap_pct", fld_from_rv("concentration.nvda_cap_pct", "pct"))
    put("nvda_current_pct", fld_from_rv("concentration.nvda_current_pct", "pct"))

    return out


__all__ = ["DerivedField", "compute_derived_inputs"]
