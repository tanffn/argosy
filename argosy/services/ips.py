"""Investment Policy Statement (IPS) — derived from the canonical plan.

The decision funnel (Stage 1 routing + Stage 3 deep decisions) must reason
against a single, explicit policy so a single-name Buy/Sell/Hold can never
silently fight the whole-portfolio plan. That policy is the IPS: target risk,
max concentration, sell discipline, retirement horizon, tax priorities — all
DERIVED from the current plan's canonical facts (``resolve_plan_numbers`` +
the canonical ``TargetAllocationDoc``), never hand-typed.

Three rules mirror the resolver's:

1. **No fabrication.** A field that can't be resolved from the plan is left
   ``None`` with ``status="pending"`` and its key appears in ``pending_keys``.
   Stage 1/3 treat a pending load-bearing field as a reason to be MORE
   conservative (default NO-OP), never to guess.
2. **Plan-derived where the plan speaks; stated-policy where it doesn't.**
   Numbers that exist in the plan (NVDA cap, sleeve targets, FI ages, tax
   retention) come from the resolver. A few genuinely-policy constraints the
   plan does not encode (general single-name cap, sell-discipline trigger
   bands) are stated here as explicit, documented constants and tagged
   ``source="ips_policy_default"`` so they are auditable and easy to change.
3. **Versioned.** ``ips_version`` is a short content hash so every funnel run
   records exactly which policy it reasoned against (D4 observability).

The IPS is read-only and cheap to build; callers rebuild it per funnel run.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from argosy.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

    from argosy.services.plan_numeric_resolver import ResolvedPlanNumbers
    from argosy.services.target_allocation_doc import TargetAllocationDoc

_log = get_logger("argosy.services.ips")


# ---------------------------------------------------------------------------
# Stated policy constants. These are genuinely-policy choices the plan does not
# encode. Single-sourced here so a change is one edit and codex-reviewable.
#   - GENERAL_SINGLE_NAME_CAP_PCT: the max weight ANY single non-NVDA name may
#     reach before the portfolio-level guard blocks an add (NVDA has its own,
#     tighter, plan-derived cap). Conservative by design — a long-hold book
#     should not silently grow a second concentrated position.
#   - SELL_TRIGGER_DRIFT_PCT / ADD_TRIGGER_DRIFT_PCT: how far a single name may
#     drift above/below its implied sleeve target before a trim/add is even
#     CONSIDERED (materiality band; ties to Stage-1 drift hard-trigger).
# ---------------------------------------------------------------------------
GENERAL_SINGLE_NAME_CAP_PCT = 10.0
SELL_TRIGGER_DRIFT_PCT = 5.0
ADD_TRIGGER_DRIFT_PCT = 5.0
# A non-US-person has no US-Israel estate treaty; US-situs assets above a $60K
# exemption are taxed up to 40%. NVDA is the one sanctioned US-situs sleeve.
NON_US_PERSON = True
SANCTIONED_US_SITUS = ("NVDA",)


@dataclass(frozen=True)
class IPSField:
    """One policy figure with its provenance.

    ``status`` is ``"resolved"`` when ``value`` traces to the plan,
    ``"policy_default"`` when it is a stated IPS constant, ``"pending"`` when
    the plan could not produce it (value is None).
    """

    value: float | None
    unit: str
    status: str  # "resolved" | "policy_default" | "pending"
    source: str

    @classmethod
    def pending(cls, unit: str, source: str) -> "IPSField":
        return cls(value=None, unit=unit, status="pending", source=source)

    @classmethod
    def policy(cls, value: float, unit: str, source: str) -> "IPSField":
        return cls(value=value, unit=unit, status="policy_default", source=source)


@dataclass(frozen=True)
class SleeveTarget:
    """One canonical allocation sleeve — the portfolio-level guard reference."""

    label: str
    sigma_class: str
    target_pct: float


@dataclass(frozen=True)
class InvestmentPolicyStatement:
    """The policy the decision funnel reasons against. Read-only, versioned."""

    user_id: str
    plan_version_id: int | None
    decision_run_id: int | None
    as_of: str | None

    # --- risk posture ---
    target_real_return_pct: IPSField
    required_real_yield_pct: IPSField

    # --- concentration / sell discipline ---
    general_single_name_cap_pct: IPSField
    nvda_cap_pct: IPSField
    nvda_target_pct: IPSField
    nvda_current_pct: IPSField
    nvda_sell_sh: IPSField
    nvda_eligible_now_sh: IPSField
    nvda_breaking_sh: IPSField
    sell_trigger_drift_pct: IPSField
    add_trigger_drift_pct: IPSField

    # --- target allocation (portfolio-level guard) ---
    sleeve_targets: list[SleeveTarget]
    equity_target_pct: IPSField
    bond_target_pct: IPSField
    cash_target_pct: IPSField

    # --- retirement horizon ---
    earliest_safe_age: IPSField
    preservation_age: IPSField
    fi_age: IPSField
    fi_crossing_year: IPSField
    fi_target_nis: IPSField
    fi_margin_signed_nis: IPSField
    liquid_net_worth_nis: IPSField
    mc_horizon_age: IPSField
    pension_unlock_age: IPSField

    # --- tax priorities ---
    retention_at_vest_pct: IPSField
    retention_capital_track_pct: IPSField
    prefer_capital_track: bool
    ucits_preferred: bool
    non_us_person: bool
    sanctioned_us_situs: tuple[str, ...]

    # --- bookkeeping ---
    pending_keys: list[str] = field(default_factory=list)
    ips_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable view (for snapshots + the trace endpoint)."""
        return asdict(self)

    @property
    def is_complete(self) -> bool:
        """True when no LOAD-BEARING field is pending (risk + concentration +
        retirement horizon). A complete IPS is required before the funnel may
        leave shadow mode for a given name."""
        return not self.pending_keys


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def _f(resolved: "ResolvedPlanNumbers", key: str, pending: list[str]) -> IPSField:
    """Lift one resolved value into an IPSField, tracking pending keys."""
    rv = resolved.get(key)
    if rv.status != "resolved" or rv.value is None:
        pending.append(key)
        return IPSField.pending(rv.unit, rv.source_locator)
    return IPSField(
        value=float(rv.value), unit=rv.unit, status="resolved",
        source=rv.source_locator,
    )


def build_ips(session: "Session", *, user_id: str) -> InvestmentPolicyStatement | None:
    """Build the IPS from the current plan. ``None`` when there is no current
    plan with a resolvable decision run (the funnel then stays fully in shadow
    / NO-OP — it never invents a policy)."""
    from argosy.services.derived_facts import build_derived_facts
    from argosy.services.plan_numeric_resolver import resolve_plan_numbers
    from argosy.services.target_allocation_doc import (
        doc_equity_bond_cash,
        load_plan_target_allocation,
    )
    from argosy.state.queries import get_current_plan

    plan = get_current_plan(session, user_id)
    if plan is None or getattr(plan, "decision_run_id", None) is None:
        _log.info("ips.no_current_plan", user_id=user_id)
        return None

    try:
        resolved = resolve_plan_numbers(
            session,
            user_id=user_id,
            decision_run_id=int(plan.decision_run_id),
            include_canonical_ages=True,
        )
    except Exception as exc:  # noqa: BLE001 — fail-soft: no IPS, full NO-OP
        _log.warning("ips.resolve_failed", user_id=user_id, error=str(exc))
        return None

    pending: list[str] = []

    # Risk posture
    target_real_return = _f(resolved, "retirement.return_assumption_pct", pending)
    required_yield = _f(resolved, "retirement.required_real_yield_pct", pending)

    # Concentration — NVDA cap/target/current/sell from the plan; general
    # single-name cap + drift bands are stated policy.
    nvda_cap = _f(resolved, "concentration.nvda_cap_pct", pending)
    nvda_target = _f(resolved, "concentration.nvda_target_pct", pending)
    nvda_current = _f(resolved, "concentration.nvda_current_pct", pending)
    nvda_sell = _f(resolved, "concentration.nvda_sell_sh", pending)
    nvda_eligible = _f(resolved, "concentration.nvda_eligible_now_sh", pending)

    # Lot-level eligibility (best-effort) from the latest tax-sim report.
    facts = None
    try:
        facts = build_derived_facts(
            session, user_id=user_id, decision_run_id=int(plan.decision_run_id)
        )
    except Exception:  # noqa: BLE001
        facts = None
    nvda_breaking = IPSField.pending("shares", "derived_facts.nvda_breaking_sh")
    if facts and facts.get("nvda_breaking_sh") is not None:
        nvda_breaking = IPSField(
            value=float(facts["nvda_breaking_sh"]), unit="shares",
            status="resolved", source="derived_facts.nvda_breaking_sh",
        )
    if facts and facts.get("nvda_eligible_now_sh") is not None and nvda_eligible.value is None:
        # Prefer the lot-derived eligibility when the resolver key was pending.
        if "concentration.nvda_eligible_now_sh" in pending:
            pending.remove("concentration.nvda_eligible_now_sh")
        nvda_eligible = IPSField(
            value=float(facts["nvda_eligible_now_sh"]), unit="shares",
            status="resolved", source="derived_facts.nvda_eligible_now_sh",
        )

    # Target allocation (portfolio-level guard reference).
    sleeves: list[SleeveTarget] = []
    eq = bnd = csh = None
    doc: "TargetAllocationDoc | None" = load_plan_target_allocation(plan)
    if doc is not None:
        sleeves = [
            SleeveTarget(label=c.label, sigma_class=c.sigma_class, target_pct=c.target_pct)
            for c in doc.classes
        ]
        try:
            eq, bnd, csh = doc_equity_bond_cash(doc)
        except Exception:  # noqa: BLE001
            eq = bnd = csh = None

    def _alloc_field(v: float | None, source: str) -> IPSField:
        if v is None:
            pending.append(source)
            return IPSField.pending("pct", source)
        return IPSField(value=float(v), unit="pct", status="resolved", source=source)

    equity_t = _alloc_field(eq, "target_allocation_doc.equity_aggregate")
    bond_t = _alloc_field(bnd, "target_allocation_doc.bond_aggregate")
    cash_t = _alloc_field(csh, "target_allocation_doc.cash_aggregate")

    # Retirement horizon.
    earliest_safe = _f(resolved, "retirement.earliest_safe_age", pending)
    preservation = _f(resolved, "retirement.preservation_age", pending)
    fi_age = _f(resolved, "retirement.fi_age", pending)
    fi_cross = _f(resolved, "retirement.fi_crossing_year", pending)
    fi_target = _f(resolved, "retirement.fi_target_nis", pending)
    fi_margin = _f(resolved, "retirement.fi_margin_signed_nis", pending)
    liquid_nw = _f(resolved, "portfolio.liquid_net_worth_nis", pending)
    mc_horizon = _f(resolved, "retirement.mc_horizon_age", pending)
    pension_unlock = _f(resolved, "retirement.pension_unlock_age", pending)

    # Tax priorities.
    ret_at_vest = _f(resolved, "tax.retention_at_vest_pct", pending)
    ret_capital = _f(resolved, "tax.retention_capital_track_pct", pending)

    as_of = None
    if getattr(plan, "accepted_at", None):
        as_of = plan.accepted_at.isoformat()
    elif getattr(plan, "imported_at", None):
        as_of = plan.imported_at.isoformat()

    ips = InvestmentPolicyStatement(
        user_id=user_id,
        plan_version_id=getattr(plan, "id", None),
        decision_run_id=int(plan.decision_run_id),
        as_of=as_of,
        target_real_return_pct=target_real_return,
        required_real_yield_pct=required_yield,
        general_single_name_cap_pct=IPSField.policy(
            GENERAL_SINGLE_NAME_CAP_PCT, "pct", "ips_policy_default"
        ),
        nvda_cap_pct=nvda_cap,
        nvda_target_pct=nvda_target,
        nvda_current_pct=nvda_current,
        nvda_sell_sh=nvda_sell,
        nvda_eligible_now_sh=nvda_eligible,
        nvda_breaking_sh=nvda_breaking,
        sell_trigger_drift_pct=IPSField.policy(
            SELL_TRIGGER_DRIFT_PCT, "pct", "ips_policy_default"
        ),
        add_trigger_drift_pct=IPSField.policy(
            ADD_TRIGGER_DRIFT_PCT, "pct", "ips_policy_default"
        ),
        sleeve_targets=sleeves,
        equity_target_pct=equity_t,
        bond_target_pct=bond_t,
        cash_target_pct=cash_t,
        earliest_safe_age=earliest_safe,
        preservation_age=preservation,
        fi_age=fi_age,
        fi_crossing_year=fi_cross,
        fi_target_nis=fi_target,
        fi_margin_signed_nis=fi_margin,
        liquid_net_worth_nis=liquid_nw,
        mc_horizon_age=mc_horizon,
        pension_unlock_age=pension_unlock,
        retention_at_vest_pct=ret_at_vest,
        retention_capital_track_pct=ret_capital,
        prefer_capital_track=True,
        ucits_preferred=True,
        non_us_person=NON_US_PERSON,
        sanctioned_us_situs=SANCTIONED_US_SITUS,
        pending_keys=pending,
    )
    return _with_version(ips)


def _with_version(ips: InvestmentPolicyStatement) -> InvestmentPolicyStatement:
    """Stamp a short content hash so every funnel run records which policy it
    reasoned against. Hashes the salient numbers + policy constants only (not
    timestamps), so an unchanged policy keeps a stable version."""
    salient = {
        "trr": ips.target_real_return_pct.value,
        "ry": ips.required_real_yield_pct.value,
        "gen_cap": ips.general_single_name_cap_pct.value,
        "nvda_cap": ips.nvda_cap_pct.value,
        "nvda_target": ips.nvda_target_pct.value,
        "sell_drift": ips.sell_trigger_drift_pct.value,
        "add_drift": ips.add_trigger_drift_pct.value,
        "eq": ips.equity_target_pct.value,
        "bnd": ips.bond_target_pct.value,
        "csh": ips.cash_target_pct.value,
        "esa": ips.earliest_safe_age.value,
        "pres": ips.preservation_age.value,
        "sleeves": [(s.label, round(s.target_pct, 4)) for s in ips.sleeve_targets],
        "prefer_capital": ips.prefer_capital_track,
        "ucits": ips.ucits_preferred,
    }
    blob = json.dumps(salient, sort_keys=True, default=str)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
    return InvestmentPolicyStatement(
        **{**asdict_shallow(ips), "ips_version": f"ips-{digest}"}
    )


def asdict_shallow(ips: InvestmentPolicyStatement) -> dict[str, Any]:
    """Shallow field map that preserves IPSField / SleeveTarget instances
    (``dataclasses.asdict`` would recurse and break re-construction)."""
    return {f_: getattr(ips, f_) for f_ in ips.__dataclass_fields__}


__all__ = [
    "IPSField",
    "InvestmentPolicyStatement",
    "SleeveTarget",
    "build_ips",
    "GENERAL_SINGLE_NAME_CAP_PCT",
    "SELL_TRIGGER_DRIFT_PCT",
    "ADD_TRIGGER_DRIFT_PCT",
]
