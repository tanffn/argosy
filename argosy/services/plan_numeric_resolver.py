"""Deterministic plan-numeric resolver — the single source of truth for
the plan's headline numbers.

This module exists to kill the user's #1 reject: the synthesizer (and the
renderer) FABRICATING round headline numbers (a ₪21M FI target, an
arbitrary retire-age, a ₪0.821M/yr savings line) that traced to nothing.

``resolve_plan_numbers`` reads the persisted state — the latest
``PortfolioSnapshotRow`` plus the per-role ``AgentReport`` rows for a
synthesis run — parses each role's ``response_text`` through its TYPED
Pydantic model, and emits one :class:`ResolvedValue` per headline key.

Three hard rules:

1. **No fabrication.** When a source row is missing, its ``response_text``
   won't parse, or a needed field is absent/None, the key resolves to
   ``status="pending"`` with ``value=None``. A constant or guess is NEVER
   substituted.
2. **Single source of truth.** The synth, the renderer, and the UI all
   read these same keys, so a number can't drift between surfaces.
3. **Resilient.** A parse failure for ONE role degrades only that role's
   keys to pending; it never crashes the resolver (logged as a warning).

The role → source registry (:data:`_RESOLVERS`) is kept as an explicit
table so the mapping from "headline key" to "agent field" can't silently
drift. Each entry knows how to turn one typed model into a set of
``ResolvedValue`` objects.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from sqlalchemy import select

from argosy.state.models import AgentReport, PortfolioSnapshotRow

if TYPE_CHECKING:  # pragma: no cover — type-checker hint only
    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedValue:
    """One headline figure, resolved to a value + a full provenance trail.

    ``status`` is ``"resolved"`` only when ``value`` is a real number
    traced to a source; otherwise ``"pending"`` and ``value is None``.
    Never carries a fabricated constant.
    """

    key: str
    value: float | None
    unit: str
    status: str  # "resolved" | "pending"
    source_locator: str
    agent_report_id: int | None = None
    confidence: str | None = None
    formula: str | None = None

    @classmethod
    def pending(
        cls,
        key: str,
        unit: str,
        source_locator: str,
        *,
        agent_report_id: int | None = None,
        formula: str | None = None,
    ) -> "ResolvedValue":
        """Build a pending sentinel — value is None, status is pending."""
        return cls(
            key=key,
            value=None,
            unit=unit,
            status="pending",
            source_locator=source_locator,
            agent_report_id=agent_report_id,
            confidence=None,
            formula=formula,
        )


@dataclass(frozen=True)
class ResolvedPlanNumbers:
    """Bag of resolved headline values, keyed by their canonical key.

    ``get`` always returns a :class:`ResolvedValue`: a pending sentinel
    when the key was never produced (so callers never KeyError and never
    have to special-case "absent" vs "pending").
    """

    values: dict[str, ResolvedValue] = field(default_factory=dict)

    def get(self, key: str) -> ResolvedValue:
        existing = self.values.get(key)
        if existing is not None:
            return existing
        unit = _KEY_UNITS.get(key, "")
        return ResolvedValue.pending(key, unit, f"{key} (never produced)")

    def __contains__(self, key: str) -> bool:  # convenience for tests
        return key in self.values


# ---------------------------------------------------------------------------
# Key registry — canonical key → unit. Used for pending sentinels so an
# absent key still reports the right unit.
# ---------------------------------------------------------------------------

_KEY_UNITS: dict[str, str] = {
    "portfolio.net_worth_nis": "nis",
    "retirement.fi_target_nis": "nis",
    "retirement.fi_age": "age",
    "retirement.earliest_safe_age": "age",
    "retirement.preservation_age": "age",
    "retirement.required_real_yield_pct": "pct",
    "retirement.return_assumption_pct": "pct",
    "spend.fi_basis_nis": "nis",
    "savings.annual_net_nis": "nis",
    "spend.annual_t12_nis": "nis",
    "concentration.nvda_cap_pct": "pct",
    "concentration.nvda_current_pct": "pct",
    "retirement.liquidity_reserve_nis": "nis",
    "retirement.fi_total_capital_nis": "nis",
    "retirement.fi_margin_signed_nis": "nis",
}


# ---------------------------------------------------------------------------
# Per-role resolvers. Each takes (parsed_json, agent_report_id) and returns
# a list of ResolvedValue. They MUST be defensive: a missing/None field
# yields a pending sentinel for that key, never a crash.
# ---------------------------------------------------------------------------


def _to_float(v: object) -> float | None:
    """Best-effort numeric coercion. None / non-numeric → None (pending)."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.replace(",", "").strip())
        except (ValueError, AttributeError):
            return None
    # Decimal, etc.
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _resolve_withdrawal_sequencer(
    data: dict, report_id: int | None
) -> list[ResolvedValue]:
    """``withdrawal_sequencer`` → WithdrawalSequencerOutput.fi_base.

    Parses through the typed model so the FiBase consistency validator
    (required_real_yield ≈ spend / target) runs — a model that would
    fail validation degrades all four keys to pending rather than
    shipping an inconsistent triple.
    """
    from argosy.agents.withdrawal_sequencer_agent import WithdrawalSequencerOutput

    keys = [
        ("retirement.fi_target_nis", "nis"),
        ("retirement.fi_age", "age"),
        ("retirement.required_real_yield_pct", "pct"),
        ("retirement.return_assumption_pct", "pct"),
        ("spend.fi_basis_nis", "nis"),
    ]
    try:
        out = WithdrawalSequencerOutput.model_validate(data)
    except Exception as exc:  # noqa: BLE001 — one bad role must not crash all
        log.warning(
            "plan_numeric_resolver.withdrawal_sequencer_parse_failed err=%s", exc
        )
        return [
            ResolvedValue.pending(k, u, f"withdrawal_sequencer.fi_base.{k.split('.')[-1]}", agent_report_id=report_id)
            for k, u in keys
        ]
    fb = out.fi_base
    conf = out.confidence.value if hasattr(out.confidence, "value") else str(out.confidence)
    return [
        ResolvedValue(
            key="retirement.fi_target_nis",
            value=_to_float(fb.fi_target_nis),
            unit="nis",
            status="resolved",
            source_locator="withdrawal_sequencer.fi_base.fi_target_nis",
            agent_report_id=report_id,
            confidence=conf,
            formula=fb.method,
        ),
        ResolvedValue(
            key="retirement.fi_age",
            value=_to_float(fb.retirement_age),
            unit="age",
            status="resolved",
            source_locator="withdrawal_sequencer.fi_base.retirement_age",
            agent_report_id=report_id,
            confidence=conf,
            formula="earliest feasible retirement age from bridge ladder + bucket unlocks",
        ),
        ResolvedValue(
            key="retirement.required_real_yield_pct",
            value=_to_float(fb.required_real_yield_pct),
            unit="pct",
            status="resolved",
            source_locator="withdrawal_sequencer.fi_base.required_real_yield_pct",
            agent_report_id=report_id,
            confidence=conf,
            formula="annual_spend_nis / fi_target_nis",
        ),
        ResolvedValue(
            key="retirement.return_assumption_pct",
            value=_to_float(fb.return_assumption_pct),
            unit="pct",
            status="resolved",
            source_locator="withdrawal_sequencer.fi_base.return_assumption_pct",
            agent_report_id=report_id,
            confidence=conf,
            formula="real (after-inflation) return assumption",
        ),
        ResolvedValue(
            key="spend.fi_basis_nis",
            value=_to_float(fb.annual_spend_nis),
            unit="nis",
            status="resolved",
            source_locator="withdrawal_sequencer.fi_base.annual_spend_nis",
            agent_report_id=report_id,
            confidence=conf,
            formula="annual household spend basis the FI target funds",
        ),
    ]


def _resolve_equity_comp_analyst(
    data: dict, report_id: int | None
) -> list[ResolvedValue]:
    """``equity_comp_analyst`` → the base (known_grants_only) scenario's
    ``five_year_avg_net_nis``.

    ``known_grants_only`` is the conservative floor — only grants on file,
    no modelled refresh. That's the right "savings.annual_net_nis" basis
    (deriving headline savings off optimistic modelled grants would be a
    soft fabrication). If the scenarios disagree materially with that
    floor, the confidence is downgraded and the spread noted in formula.
    """
    from argosy.agents.equity_comp_analyst_types import EquityCompAnalystOutput

    key = "savings.annual_net_nis"
    loc = "equity_comp_analyst.scenarios[known_grants_only].five_year_avg_net_nis"
    try:
        out = EquityCompAnalystOutput.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "plan_numeric_resolver.equity_comp_analyst_parse_failed err=%s", exc
        )
        return [ResolvedValue.pending(key, "nis", loc, agent_report_id=report_id)]

    by_name = {s.name: s for s in out.scenarios}
    base = by_name.get("known_grants_only")
    if base is None:
        return [ResolvedValue.pending(key, "nis", loc, agent_report_id=report_id)]

    value = _to_float(base.five_year_avg_net_nis)
    if value is None:
        return [ResolvedValue.pending(key, "nis", loc, agent_report_id=report_id)]

    conf = base.confidence
    formula = "5-yr mean net_nis, known_grants_only scenario (conservative floor)"
    # Note the spread if other scenarios disagree materially with the floor.
    others = [
        _to_float(s.five_year_avg_net_nis)
        for s in out.scenarios
        if s.name != "known_grants_only"
    ]
    others = [o for o in others if o is not None]
    if others and value > 0:
        spread = max(abs(o - value) / value for o in others)
        if spread > 0.25:
            conf = "LOW"
            formula += f"; scenarios disagree (max spread {spread * 100:.0f}% vs floor)"

    return [
        ResolvedValue(
            key=key,
            value=value,
            unit="nis",
            status="resolved",
            source_locator=loc,
            agent_report_id=report_id,
            confidence=conf,
            formula=formula,
        )
    ]


def _resolve_household_budget(
    data: dict, report_id: int | None
) -> list[ResolvedValue]:
    """``household_budget`` → HouseholdBudgetReport.monthly_burn_nis × 12."""
    from argosy.agents.household_budget_analyst import HouseholdBudgetReport

    key = "spend.annual_t12_nis"
    loc = "household_budget.monthly_burn_nis * 12"
    try:
        out = HouseholdBudgetReport.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "plan_numeric_resolver.household_budget_parse_failed err=%s", exc
        )
        return [ResolvedValue.pending(key, "nis", loc, agent_report_id=report_id)]

    monthly = _to_float(out.monthly_burn_nis)
    # 0.0 is the schema default — treat a non-positive burn as "not produced"
    # rather than asserting a household with zero spend.
    if monthly is None or monthly <= 0:
        return [ResolvedValue.pending(key, "nis", loc, agent_report_id=report_id)]
    conf = out.confidence.value if hasattr(out.confidence, "value") else str(out.confidence)
    return [
        ResolvedValue(
            key=key,
            value=monthly * 12.0,
            unit="nis",
            status="resolved",
            source_locator=loc,
            agent_report_id=report_id,
            confidence=conf,
            formula="monthly_burn_nis * 12 (tracked T12 household burn)",
        )
    ]


def _resolve_concentration(
    data: dict, report_id: int | None
) -> list[ResolvedValue]:
    """``concentration`` → ConcentrationAnalystOutput nvda cap + current."""
    from argosy.agents.concentration_analyst_types import ConcentrationAnalystOutput

    keys = [
        ("concentration.nvda_cap_pct", "nvda_cap_pct"),
        ("concentration.nvda_current_pct", "current_nvda_pct"),
    ]
    try:
        out = ConcentrationAnalystOutput.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "plan_numeric_resolver.concentration_parse_failed err=%s", exc
        )
        return [
            ResolvedValue.pending(k, "pct", f"concentration.{attr}", agent_report_id=report_id)
            for k, attr in keys
        ]
    conf = out.confidence.value if hasattr(out.confidence, "value") else str(out.confidence)
    return [
        ResolvedValue(
            key="concentration.nvda_cap_pct",
            value=_to_float(out.nvda_cap_pct),
            unit="pct",
            status="resolved",
            source_locator="concentration.nvda_cap_pct",
            agent_report_id=report_id,
            confidence=conf,
            formula="MIN over four constraint caps (sequence/tail/risk/tax)",
        ),
        ResolvedValue(
            key="concentration.nvda_current_pct",
            value=_to_float(out.current_nvda_pct),
            unit="pct",
            status="resolved",
            source_locator="concentration.current_nvda_pct",
            agent_report_id=report_id,
            confidence=conf,
            formula="current NVDA share of tradeable portfolio (snapshot-derived)",
        ),
    ]


# Explicit registry — role name → (keys it owns, resolver fn). Keep as a
# dict so the mapping can't drift; `_KEY_UNITS` above mirrors the keys.
_RESOLVERS: dict[str, tuple[tuple[str, ...], Callable[[dict, int | None], list[ResolvedValue]]]] = {
    "withdrawal_sequencer": (
        (
            "retirement.fi_target_nis",
            "retirement.fi_age",
            "retirement.required_real_yield_pct",
            "retirement.return_assumption_pct",
            "spend.fi_basis_nis",
        ),
        _resolve_withdrawal_sequencer,
    ),
    "equity_comp_analyst": (
        ("savings.annual_net_nis",),
        _resolve_equity_comp_analyst,
    ),
    "household_budget": (
        ("spend.annual_t12_nis",),
        _resolve_household_budget,
    ),
    "concentration": (
        ("concentration.nvda_cap_pct", "concentration.nvda_current_pct"),
        _resolve_concentration,
    ),
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _current_boi_usd_nis(
    session: "Session", snapshot_fx: float
) -> tuple[float | None, str]:
    """The ONE USD/NIS mark for marking USD→NIS across the whole book.

    Returns ``(rate, source_label)``. Prefers the CURRENT BOI USD/NIS via the
    cache-only walkback; falls back to the snapshot's stored ``fx_usd_nis``
    ONLY when BOI is uncached. Both net worth and US-situs estate call this so
    there is a single FX convention (prevents the stale-snapshot-vs-current-BOI
    divergence that understated the estate tail). Returns ``(None, ...)`` when
    neither a BOI rate nor a positive snapshot fx is available.
    """
    from datetime import date

    snap_fx = snapshot_fx if snapshot_fx and snapshot_fx > 0 else 0.0
    try:
        from argosy.services.fx import cache as _fxcache
        rate = float(_fxcache.find_walkback(session, date.today(), "USD", max_days=10))
        if rate > 0:
            return rate, "BOI current USD/NIS"
    except Exception:  # noqa: BLE001 — uncached / unavailable → snapshot fallback
        pass
    if snap_fx > 0:
        return snap_fx, "snapshot fx (BOI uncached)"
    return None, "no FX available"


def _resolve_net_worth(
    session: "Session", user_id: str
) -> ResolvedValue:
    """Net worth in NIS, marked to the CURRENT BOI USD/NIS rate.

    The household holds ~USD assets but spends NIS, so the decision-relevant
    figure is current NIS purchasing power: USD-denominated holdings × the
    latest BOI USD/NIS + NIS-origin cash in native shekels (NOT re-translated as
    USD exposure). This replaces using the snapshot's stored fx_usd_nis, which
    for the dev snapshot was 2.94 — an erroneous value matching neither its date
    nor current BOI (codex FX review 2026-06-04). Falls back to the snapshot fx
    only if BOI is uncached. Holdings remain as-of the snapshot date (provisional
    until refreshed). Pending when no snapshot/value exists — never fabricated.
    """
    key = "portfolio.net_worth_nis"
    try:
        snap = session.execute(
            select(PortfolioSnapshotRow)
            .where(PortfolioSnapshotRow.user_id == user_id)
            .order_by(PortfolioSnapshotRow.id.desc())
            .limit(1)
        ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001 — defensive
        log.warning("plan_numeric_resolver.snapshot_query_failed err=%s", exc)
        snap = None
    if snap is None:
        return ResolvedValue.pending(key, "nis", "portfolio_snapshot (none)")

    snap_fx = _to_float(snap.fx_usd_nis) or 0.0
    # Current BOI rate (cache-only walkback); fall back to the snapshot fx.
    fx, fx_src = _current_boi_usd_nis(session, snap_fx)
    if not fx or fx <= 0:
        return ResolvedValue.pending(key, "nis", "no FX available")

    # Currency split from positions: USD assets × current FX + NIS native.
    usd_assets_usd = 0.0
    nis_native_nis = 0.0
    try:
        positions = json.loads(snap.positions_json or "[]")
    except (json.JSONDecodeError, ValueError, TypeError):
        positions = []
    for p in positions:
        v = _to_float(p.get("usd_value_k")) or 0.0
        if (p.get("currency") or "").upper() == "USD":
            usd_assets_usd += v * 1000.0
        else:
            nis_native_nis += v * 1000.0 * (snap_fx if snap_fx > 0 else fx)

    holdings_as_of = getattr(snap, "snapshot_date", None)
    as_of = holdings_as_of.isoformat() if holdings_as_of else f"snapshot id={snap.id}"
    if usd_assets_usd > 0 or nis_native_nis > 0:
        value = usd_assets_usd * fx + nis_native_nis
        loc = (
            f"USD assets ${usd_assets_usd/1e6:.2f}M × {fx_src} {fx:.3f} + "
            f"NIS-native ₪{nis_native_nis:,.0f}; holdings as of {as_of} (provisional)"
        )
        formula = "USD-denominated assets × current BOI USD/NIS + NIS-native cash"
    else:
        # No per-position currencies → fall back to totals × current FX.
        try:
            totals = json.loads(snap.totals_json or "{}")
        except (json.JSONDecodeError, ValueError, TypeError):
            totals = {}
        total_usd_k = _to_float(totals.get("total_usd_value_k"))
        if not total_usd_k or total_usd_k <= 0:
            return ResolvedValue.pending(key, "nis", "snapshot has no positions/totals")
        value = total_usd_k * 1000.0 * fx
        loc = f"total_usd_value_k ${total_usd_k/1e3:.2f}M × {fx_src} {fx:.3f}; holdings as of {as_of} (provisional)"
        formula = "total_usd_value_k * 1000 * current BOI USD/NIS"

    return ResolvedValue(
        key=key, value=value, unit="nis", status="resolved",
        source_locator=loc, agent_report_id=None, confidence="HIGH", formula=formula,
    )


def resolve_plan_numbers(
    session: "Session", *, user_id: str, decision_run_id: int,
    include_canonical_ages: bool = False,
) -> ResolvedPlanNumbers:
    """Resolve all plan headline numbers for one synthesis run.

    Reads the latest portfolio snapshot (net worth) plus the per-role
    ``AgentReport`` rows stamped ``decision_id='plan-synth-<id>'`` and
    parses each through its typed model. Every headline key resolves to a
    :class:`ResolvedValue`; absent / unparseable / missing-field inputs
    resolve to ``status="pending"`` with ``value=None`` — never a guess.

    A parse failure for one role degrades only that role's keys; the
    resolver never raises.

    ``include_canonical_ages`` (default ``False``) adds the canonical
    dual-track retirement ages (``retirement.earliest_safe_age`` +
    ``retirement.preservation_age``) from
    ``retirement_plan.canonical_feasible_dual_track`` — the SAME basis the
    /retirement headline + ruin hero bind to. It is OFF by default for two
    reasons: (1) that engine runs a heavy MC; (2) it is mutually re-entrant
    with this resolver (``canonical_feasible_dual_track`` →
    ``resolve_canonical_basis`` → ``_nvda_deconcentration_haircut`` →
    ``resolve_plan_numbers``), so it must only ever be requested by a
    TOP-LEVEL display surface (the /plan narrative + the synth numbers block),
    never from the re-entrant hop. The keys stay pending on any failure.
    """
    values: dict[str, ResolvedValue] = {}

    # Snapshot-derived net worth.
    nw = _resolve_net_worth(session, user_id)
    values[nw.key] = nw

    decision_id = f"plan-synth-{decision_run_id}"

    for role, (keys, fn) in _RESOLVERS.items():
        # Latest report for this role within the run (highest id wins).
        report = None
        try:
            report = session.execute(
                select(AgentReport)
                .where(AgentReport.decision_id == decision_id)
                .where(AgentReport.agent_role == role)
                .order_by(AgentReport.id.desc())
                .limit(1)
            ).scalar_one_or_none()
        except Exception as exc:  # noqa: BLE001 — defensive
            log.warning(
                "plan_numeric_resolver.report_query_failed role=%s err=%s",
                role, exc,
            )
            report = None

        if report is None:
            # Row missing → every key this role owns is pending (no fabrication).
            for k in keys:
                loc = f"{role} (no agent_report for {decision_id})"
                values[k] = ResolvedValue.pending(k, _KEY_UNITS.get(k, ""), loc)
            continue

        # Parse response_text JSON. Bad JSON → role's keys pending.
        # Use the same lenient parser the live agent uses
        # (``BaseAgent._parse_output``): the persisted ``response_text``
        # is the model's verbatim output, which several roles (e.g.
        # ``concentration``, ``fund_manager``) wrap in a ```json fence.
        # Bare ``json.loads`` chokes on the fence and silently degraded
        # those keys to pending (the NVDA cap never resolved).
        try:
            from argosy.agents._json_parse import lenient_json_loads
            parsed = lenient_json_loads(report.response_text or "")
        except (json.JSONDecodeError, ValueError, TypeError):
            log.warning(
                "plan_numeric_resolver.response_text_not_json role=%s report_id=%s",
                role, report.id,
            )
            for k in keys:
                loc = f"{role} (response_text not JSON, report_id={report.id})"
                values[k] = ResolvedValue.pending(
                    k, _KEY_UNITS.get(k, ""), loc, agent_report_id=report.id
                )
            continue

        if not isinstance(parsed, dict):
            for k in keys:
                loc = f"{role} (response_text not a JSON object, report_id={report.id})"
                values[k] = ResolvedValue.pending(
                    k, _KEY_UNITS.get(k, ""), loc, agent_report_id=report.id
                )
            continue

        try:
            resolved = fn(parsed, report.id)
        except Exception as exc:  # noqa: BLE001 — a resolver bug for one role
            log.warning(
                "plan_numeric_resolver.resolver_raised role=%s report_id=%s err=%s",
                role, report.id, exc,
            )
            resolved = [
                ResolvedValue.pending(
                    k, _KEY_UNITS.get(k, ""),
                    f"{role} (resolver error, report_id={report.id})",
                    agent_report_id=report.id,
                )
                for k in keys
            ]
        for rv in resolved:
            values[rv.key] = rv

    # ------------------------------------------------------------------
    # Deterministic FI methodology — the SINGLE SOURCE OF TRUTH for the FI
    # capital target, spend basis, and required yield. These OVERRIDE the
    # LLM withdrawal_sequencer's fi_base values: the headline FI number must
    # be DERIVED deterministically (permanent-equivalent spend ÷ a defensible
    # after-tax perpetual SWR), never invented by the model. The agent keeps
    # only ``retirement.fi_age`` (the trajectory-feasibility number it
    # genuinely derives). See argosy.services.fi_methodology.
    # ------------------------------------------------------------------
    _apply_fi_methodology(session, user_id, values)
    _apply_us_situs_estate(session, user_id, values)
    _apply_nvda_current_weight(session, user_id, values)
    _apply_fx_boi(session, values)

    # ONE signed FI sufficiency margin (net_worth − FI-total-capital). Computed
    # once here so every surface cites the SAME signed number — the
    # reached/not-reached sign can never diverge across surfaces again.
    _apply_fi_margin(values)

    # Canonical dual-track retirement ages — DISPLAY surfaces only (see the
    # docstring re: re-entrancy + MC cost). Gated so the re-entrant NVDA-haircut
    # hop and the non-display callers never trigger the heavy canonical MC.
    if include_canonical_ages:
        _apply_canonical_dual_track_age(session, user_id, values)
        _apply_canonical_mc_spend(session, user_id, values)
        if decision_run_id is not None:
            _apply_canonical_allocation(session, decision_run_id, values)

    return ResolvedPlanNumbers(values=values)


def _apply_canonical_mc_spend(
    session: "Session", user_id: str, values: dict[str, ResolvedValue]
) -> None:
    """Register the Monte-Carlo central + stress spend as RESOLVED values from
    ``resolve_canonical_basis`` — the SAME basis the ruin hero / scenario grid /
    dual-track age bind to. Without this the synth has no manifest source for the
    MC central spend and cites it to a project-memory note, which the fund
    manager (correctly) rejects as unsupported. The MC central spend is a
    DISTINCT concept from the T12 trailing-actual spend and the FI
    permanent-equivalent spend — all three are legitimately different; this
    makes the MC one Argosy-derived + auditable so the prose can cite it.
    """
    try:
        from argosy.services.retirement.retirement_plan import (
            resolve_canonical_basis,
        )

        basis = resolve_canonical_basis(session=session, user_id=user_id)
    except Exception as exc:  # noqa: BLE001 — best-effort, never break the resolver
        log.warning("plan_numeric_resolver.mc_spend_failed err=%s", exc)
        return
    for attr, key, label in (
        ("spend_central_nis", "spend.mc_central_nis", "MC central spend"),
        ("spend_stress_nis", "spend.mc_stress_nis", "MC stress spend"),
    ):
        val = _to_float(getattr(basis, attr, None))
        if val is not None:
            values[key] = ResolvedValue(
                key=key, value=val, unit="nis", status="resolved",
                source_locator=f"retirement_plan.resolve_canonical_basis.{attr}",
                confidence="HIGH",
                formula=f"{label} (canonical MC basis — ruin hero / scenario grid)",
            )


def _apply_canonical_dual_track_age(
    session: "Session", user_id: str, values: dict[str, ResolvedValue]
) -> None:
    """Resolve the CANONICAL dual-track retirement ages — the ONE honest
    earliest-safe (typical-drawdown) age + the capital-preservation what-if —
    from ``retirement_plan.canonical_feasible_dual_track`` (the same basis the
    /retirement headline, ruin hero, and scenario grid bind to). This is what
    makes the /plan narrative + synthesizer state the SAME age as /retirement
    instead of the stale ``retirement.fi_age`` (the trajectory-feasibility
    number, kept intact for its legitimate FIRE-bridge-sizing consumers).

    Lazy import: the retirement engine and this resolver are mutually
    re-entrant, so the call is only reached from a top-level display surface
    (the caller gates it behind ``include_canonical_ages``). Best-effort — any
    failure (thin data, MC error, or no age clearing the bar) leaves the keys
    absent → pending sentinel. NEVER a fabricated or stale fallback value.
    """
    early_key = "retirement.earliest_safe_age"
    pres_key = "retirement.preservation_age"
    try:
        from argosy.services.retirement.retirement_plan import (
            canonical_feasible_dual_track,
        )

        canon = canonical_feasible_dual_track(session=session, user_id=user_id)
    except Exception as exc:  # noqa: BLE001 — resolver must not break on this
        log.warning(
            "plan_numeric_resolver.canonical_age_failed user_id=%s err=%s",
            user_id, exc,
        )
        return

    early_age = _to_float(getattr(canon, "earliest_feasible_age", None))
    p_solvent = getattr(canon, "p_solvent_at_age", None)
    if early_age is not None:
        target_p = getattr(canon, "target_p_solvent", 0.90) or 0.90
        values[early_key] = ResolvedValue(
            key=early_key,
            value=early_age,
            unit="age",
            status="resolved",
            source_locator="retirement_plan.canonical_feasible_dual_track.earliest_feasible_age",
            confidence="HIGH" if p_solvent is not None else "MEDIUM",
            formula=(
                "earliest age the typical-regime drawdown Monte Carlo clears "
                f"{target_p:.0%} solvency to 95 on the deconcentrated, "
                "reserve-netted, CGT-haircut basis"
            ),
        )

    basis = getattr(canon, "basis", None)
    pres_age = _to_float(basis.get("preservation_age")) if isinstance(basis, dict) else None
    if pres_age is not None:
        values[pres_key] = ResolvedValue(
            key=pres_key,
            value=pres_age,
            unit="age",
            status="resolved",
            source_locator="retirement_plan.canonical_feasible_dual_track.basis.preservation_age",
            confidence="MEDIUM",
            formula="earliest age the worst-10% real path preserves today's real principal to 95",
        )


def _slug(label: str) -> str:
    """Compact ascii slug for a canonical allocation-class label."""
    import re as _re

    return _re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:48]


def _apply_canonical_allocation(
    session: "Session", decision_run_id: int, values: dict[str, ResolvedValue]
) -> None:
    """Register the canonical TargetAllocationDoc weights + structural ages as
    RESOLVED values, so the headline-numeric-source gate can trace every
    Argosy-derived allocation number the plan prose cites (the NVDA target,
    each asset-class target %, the concentration cap) instead of flagging them
    as fabrications.

    Source: the persisted ``target_allocation_json`` on the plan version that
    THIS decision run produced — i.e. the exact doc the prose was rendered
    from. Best-effort; any failure leaves the keys absent (the gate then flags
    the numbers, which is the safe direction). Percent values are stored as
    FRACTIONS to match the resolver's pct convention (the gate scales ×100).
    """
    from argosy.state.models import PlanVersion

    try:
        pv = session.execute(
            select(PlanVersion)
            .where(PlanVersion.decision_run_id == decision_run_id)
            .order_by(PlanVersion.id.desc())
        ).scalars().first()
    except Exception as exc:  # noqa: BLE001
        log.warning("plan_numeric_resolver.alloc_lookup_failed err=%s", exc)
        return
    if pv is None or not pv.target_allocation_json:
        return
    try:
        import json as _json

        doc = _json.loads(pv.target_allocation_json)
    except Exception:  # noqa: BLE001
        return

    # Per-class target weights (percent-points in the doc → fraction here).
    for cls in doc.get("classes", []) or []:
        label = cls.get("label") or cls.get("class_label") or cls.get("name")
        tgt = cls.get("target_pct")
        if label is None or tgt is None:
            continue
        key = f"allocation.{_slug(label)}_target_pct"
        values[key] = ResolvedValue(
            key=key,
            value=float(tgt) / 100.0,
            unit="pct",
            status="resolved",
            source_locator=f"target_allocation_doc.classes[{label!r}].target_pct",
            confidence="HIGH",
            formula="canonical TargetAllocationDoc strategic class weight",
        )

    # NVDA concentration cap (doc carries percent-points). The canonical doc's
    # cap is the USER-SETTLED BINDING cap (13%) and OVERRIDES the concentration
    # analyst's tail-loss-derived value (~7%): the analyst's number is a
    # subordinate, more-conservative input, but the binding cap the plan states
    # is the settled 13%. Without the override the manifest reports 7% and the
    # gate false-flags the plan's correct "13% cap" as a fabrication.
    cap = doc.get("nvda_cap_pct")
    if cap is not None:
        # Preserve the concentration analyst's derived cap (the prior value,
        # ~7% MIN-over-constraints) under a distinct key so the plan can cite
        # BOTH the binding cap (13%) and the analyst's more-conservative floor
        # (~7%) and both trace — they are two legitimate Argosy-derived numbers.
        prior = values.get("concentration.nvda_cap_pct")
        if (
            prior is not None
            and prior.status == "resolved"
            and prior.value is not None
        ):
            values["concentration.nvda_analyst_floor_pct"] = ResolvedValue(
                key="concentration.nvda_analyst_floor_pct",
                value=float(prior.value),
                unit="pct",
                status="resolved",
                source_locator=prior.source_locator,
                confidence=prior.confidence,
                formula="concentration analyst MIN-over-constraints cap (subordinate floor to the binding cap)",
            )
        values["concentration.nvda_cap_pct"] = ResolvedValue(
            key="concentration.nvda_cap_pct",
            value=float(cap) / 100.0,
            unit="pct",
            status="resolved",
            source_locator="target_allocation_doc.nvda_cap_pct (canonical binding cap)",
            confidence="HIGH",
            formula="canonical user-settled concentration cap (overrides analyst tail-loss)",
        )

    # Structural ages the prose legitimately cites — statutory + MC horizon
    # constants, not fabrications: 60 (keren/kupot partial unlock), 67
    # (statutory pension age), 95 (Monte-Carlo solvency horizon).
    for age, key, why in (
        (60.0, "statutory.pension_unlock_age", "age-60 keren/kupot partial unlock"),
        (67.0, "statutory.retirement_age", "statutory pension age 67"),
        (95.0, "mc.solvency_horizon_age", "Monte-Carlo solvency horizon"),
    ):
        if key not in values:
            values[key] = ResolvedValue(
                key=key, value=age, unit="age", status="resolved",
                source_locator=f"statutory_constant ({why})",
                confidence="HIGH", formula=why,
            )


def _apply_fx_boi(session: "Session", values: dict[str, ResolvedValue]) -> None:
    """Resolve USD/NIS from the authoritative Bank-of-Israel feed (the FxRate
    cache, walking back over weekends/holidays), plus a 90-day band. This is the
    FX source of truth — the assumption-ledger FX rows (A5/A6) and the synth bind
    to it instead of a hardcoded 3.45 that contradicted the actual BOI rate the
    agents computed at (~2.81). Pending (never the magic number) when no rate is
    cached.
    """
    from datetime import date, timedelta

    key = "fx.usd_nis"
    loc = "boi USD/NIS daily representative rate (FxRate cache, walkback)"
    try:
        # Cache-only read (warmed by the FX refresh job) — no live network in
        # the resolver hot path; pending if the cache is cold.
        from argosy.services.fx import cache as _fxcache
        today = date.today()
        rate = float(_fxcache.find_walkback(session, today, "USD", max_days=10))
        from argosy.state.models import FxRate
        since = today - timedelta(days=90)
        band_rows = session.execute(
            select(FxRate.rate).where(
                FxRate.currency == "USD", FxRate.date >= since,
            )
        ).scalars().all()
        band = [float(x) for x in band_rows] if band_rows else [rate]
        lo, hi = min(band), max(band)
        values[key] = ResolvedValue(
            key=key, value=rate, unit="nis_per_usd", status="resolved",
            source_locator=loc, agent_report_id=None, confidence="HIGH",
            formula=f"Bank of Israel representative USD/NIS; 90-day band {lo:.3f}–{hi:.3f}",
        )
        values["fx.usd_nis_band_low"] = ResolvedValue(
            key="fx.usd_nis_band_low", value=lo, unit="nis_per_usd", status="resolved",
            source_locator="boi USD/NIS 90-day low", agent_report_id=None, confidence="HIGH",
        )
        values["fx.usd_nis_band_high"] = ResolvedValue(
            key="fx.usd_nis_band_high", value=hi, unit="nis_per_usd", status="resolved",
            source_locator="boi USD/NIS 90-day high", agent_report_id=None, confidence="HIGH",
        )
    except Exception as exc:  # noqa: BLE001 — no cached rate → pending, never 3.45
        log.warning("plan_numeric_resolver.fx_boi_unavailable err=%s", exc)
        values[key] = ResolvedValue.pending(key, "nis_per_usd", loc)


def _apply_us_situs_estate(
    session: "Session", user_id: str, values: dict[str, ResolvedValue]
) -> None:
    """Derive US-situs estate exposure from the snapshot positions via the
    canonical IRS-NRA classifier (safety_gates._us_situs_assets_usd), which
    classifies each position by instrument DOMICILE
    (instrument_reference.estate_safe_for) — every US-domiciled security at any
    broker (NVDA + US ETFs + US single names, at Schwab AND the Israeli broker);
    UCITS / Israeli instruments and cash excluded — converted to NIS. The synth
    previously AUTHORED this number (FM caught a fabrication); feeding the
    derived value kills it. Pending (never guessed) when the snapshot is
    missing or empty.
    """
    key = "concentration.us_situs_estate_exposure_nis"
    loc = (
        "safety_gates._us_situs_assets_usd(snapshot positions) × current BOI "
        "USD/NIS (snapshot fx fallback)"
    )
    try:
        from argosy.services.retirement.safety_gates import _us_situs_assets_usd

        snap = session.execute(
            select(PortfolioSnapshotRow)
            .where(PortfolioSnapshotRow.user_id == user_id)
            .order_by(PortfolioSnapshotRow.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if snap is None:
            values[key] = ResolvedValue.pending(key, "nis", loc)
            return
        try:
            positions = json.loads(snap.positions_json or "[]")
        except (json.JSONDecodeError, ValueError, TypeError):
            positions = []
        usd = _us_situs_assets_usd(positions)
        # Mark to the SAME current-BOI-FX basis net worth uses (snapshot fx is
        # the fallback only when BOI is uncached) — one FX convention per book.
        snap_fx = _to_float(snap.fx_usd_nis) or 0.0
        fx, fx_src = _current_boi_usd_nis(session, snap_fx)
        if not usd or not fx or usd <= 0 or fx <= 0:
            values[key] = ResolvedValue.pending(key, "nis", loc)
            return
        values[key] = ResolvedValue(
            key=key,
            value=usd * fx,
            unit="nis",
            status="resolved",
            source_locator=f"{loc} = {fx_src} {fx:.3f} (snapshot id={snap.id})",
            agent_report_id=None,
            confidence="HIGH",
            formula=(
                "Σ US-domiciled securities across ALL brokers (by instrument "
                "domicile: NVDA + US ETFs + US single names at Schwab and the "
                "Israeli broker; UCITS / Israeli / cash excluded) per IRS NRA "
                "estate-tax rules, × current BOI USD/NIS (snapshot fx fallback)"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — defensive; leave pending
        log.warning("plan_numeric_resolver.us_situs_failed err=%s", exc)
        values[key] = ResolvedValue.pending(key, "nis", loc)


def _apply_nvda_current_weight(
    session: "Session", user_id: str, values: dict[str, ResolvedValue]
) -> None:
    """Override ``concentration.nvda_current_pct`` with a DETERMINISTIC,
    snapshot-derived value (stored as a fraction 0–1, matching the unit
    convention).

    The current NVDA weight is a mechanical fact, not a judgment — yet it was
    sourced from the LLM concentration analyst's ``current_nvda_pct`` and went
    "[derivation pending]" whenever that agent's output failed validation. That
    fragility is one leg of the "NVDA weight reported three ways" defect. Here
    we compute it deterministically as NVDA ÷ tradeable securities book — the
    SAME canonical definition the wealth dashboard uses (one number across
    surfaces) — so it is never pending and always auditable. The analyst still
    owns the cap (a genuine judgment); only the current weight is overridden.
    """
    key = "concentration.nvda_current_pct"
    loc = "wealth_dashboard.nvda_concentration_pct(snapshot positions)"
    try:
        from argosy.services.wealth_dashboard import nvda_concentration_pct

        snap = session.execute(
            select(PortfolioSnapshotRow)
            .where(PortfolioSnapshotRow.user_id == user_id)
            .order_by(PortfolioSnapshotRow.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if snap is None:
            return  # leave whatever the role resolver set (likely pending)
        try:
            positions = json.loads(snap.positions_json or "[]")
        except (json.JSONDecodeError, ValueError, TypeError):
            return
        pct = nvda_concentration_pct(positions)
        if pct is None:
            return
        values[key] = ResolvedValue(
            key=key,
            value=pct / 100.0,  # stored as a fraction (0–1), unit "pct"
            unit="pct",
            status="resolved",
            source_locator=f"{loc} (snapshot id={snap.id})",
            agent_report_id=None,
            confidence="HIGH",
            formula=(
                "NVDA usd_value_k ÷ tradeable securities book (excl. cash + "
                "physical real estate), snapshot-derived — % of tradeable book"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — defensive; leave prior value
        log.warning("plan_numeric_resolver.nvda_current_weight_failed err=%s", exc)


def _apply_fi_margin(values: dict[str, ResolvedValue]) -> None:
    """Single signed FI sufficiency margin = net_worth − FI-total-capital.

    Positive => the total capital target is (marginally) reached. Every surface
    that states 'FI reached / not reached' MUST cite this ONE value, so the
    reached/not-reached sign can never diverge across surfaces (the
    'reached vs −118,020 not-reached' contradiction was two surfaces computing
    the margin independently with opposite sign conventions). Pending (never a
    guess) when either input is unresolved.
    """
    key = "retirement.fi_margin_signed_nis"
    loc = "portfolio.net_worth_nis − retirement.fi_total_capital_nis"
    nw = values.get("portfolio.net_worth_nis")
    tot = values.get("retirement.fi_total_capital_nis")
    if (
        nw is None
        or tot is None
        or nw.status != "resolved"
        or tot.status != "resolved"
        or nw.value is None
        or tot.value is None
    ):
        values[key] = ResolvedValue.pending(key, "nis", loc)
        return
    values[key] = ResolvedValue(
        key=key,
        value=float(nw.value) - float(tot.value),
        unit="nis",
        status="resolved",
        source_locator=loc,
        agent_report_id=None,
        confidence="HIGH",
        formula="net_worth_nis − fi_total_capital_nis (signed; >0 => total target reached)",
    )


def _apply_fi_methodology(
    session: "Session", user_id: str, values: dict[str, ResolvedValue]
) -> None:
    """Override the FI capital/spend/yield keys with the deterministic
    methodology. The tracked T12 (already resolved from household_budget) is
    fed in as the spend basis when available; otherwise the service reads it
    from identity_yaml. A failure leaves whatever the agent produced (the
    agent values are still derived, just not methodology-corrected) — never
    raises.
    """
    try:
        from argosy.services.fi_methodology import compute_fi_target

        t12_rv = values.get("spend.annual_t12_nis")
        t12 = (
            float(t12_rv.value)
            if t12_rv is not None and t12_rv.status == "resolved" and t12_rv.value
            else None
        )
        m = compute_fi_target(session, user_id=user_id, spend_t12_nis=t12)
    except Exception as exc:  # noqa: BLE001 — defensive; agent values stand
        log.warning("plan_numeric_resolver.fi_methodology_failed err=%s", exc)
        return
    if m is None:
        return

    conf = m.confidence
    values["retirement.fi_target_nis"] = ResolvedValue(
        key="retirement.fi_target_nis",
        value=float(m.fi_perpetuity_nis),
        unit="nis",
        status="resolved",
        source_locator="fi_methodology.fi_perpetuity_nis (permanent_spend / SWR)",
        agent_report_id=None,
        confidence=conf,
        formula=m.method,
    )
    values["spend.fi_basis_nis"] = ResolvedValue(
        key="spend.fi_basis_nis",
        value=float(m.permanent_annual_spend_nis),
        unit="nis",
        status="resolved",
        source_locator="fi_methodology.permanent_annual_spend_nis",
        agent_report_id=None,
        confidence=conf,
        formula="tracked baseline (ex-mortgage) + amortized life-event spend",
    )
    values["retirement.required_real_yield_pct"] = ResolvedValue(
        key="retirement.required_real_yield_pct",
        value=float(m.swr_real_pct),
        unit="pct",
        status="resolved",
        source_locator="fi_methodology.swr_real_pct (perpetual real after-tax SWR)",
        agent_report_id=None,
        confidence=conf,
        formula=f"defensible perpetual SWR; band {m.swr_band[0]*100:.1f}-{m.swr_band[1]*100:.1f}%",
    )
    values["retirement.return_assumption_pct"] = ResolvedValue(
        key="retirement.return_assumption_pct",
        value=float(m.return_assumption_real_pct),
        unit="pct",
        status="resolved",
        source_locator="fi_methodology.return_assumption_real_pct",
        agent_report_id=None,
        confidence=conf,
        formula="expected real return for the trajectory (decoupled from the SWR)",
    )
    values["retirement.fi_total_capital_nis"] = ResolvedValue(
        key="retirement.fi_total_capital_nis",
        value=float(m.fi_total_capital_nis),
        unit="nis",
        status="resolved",
        source_locator="fi_methodology.fi_total_capital_nis (perpetuity + reserve)",
        agent_report_id=None,
        confidence=conf,
        formula="FI perpetuity + finite-liability reserve (the full capital target)",
    )
    values["retirement.liquidity_reserve_nis"] = ResolvedValue(
        key="retirement.liquidity_reserve_nis",
        value=float(m.finite_liability_reserve_nis),
        unit="nis",
        status="resolved",
        source_locator="fi_methodology.finite_liability_reserve_nis",
        agent_report_id=None,
        confidence=conf,
        formula="education + mortgage runoff + wedding lumps (NOT capitalized into perpetuity)",
    )

    # FIRE bridge — the liquid capital that funds the permanent-equivalent spend
    # from retirement to the age-60 pension unlock. DERIVED here (not authored by
    # the LLM) and at the permanent-equivalent basis, so the synth is fed it +
    # the scrub can source it (codex residual: it was LLM-stated at the T12 burn).
    bridge_key = "retirement.fire_bridge_nis"
    fi_age_rv = values.get("retirement.fi_age")
    fi_age = (
        float(fi_age_rv.value)
        if (fi_age_rv is not None and fi_age_rv.status == "resolved" and fi_age_rv.value is not None)
        else None
    )
    if fi_age is not None:
        from argosy.services.cashflow_projection import LUMP_PENSION_AGE
        bridge_years = max(0.0, float(LUMP_PENSION_AGE) - fi_age)
        values[bridge_key] = ResolvedValue(
            key=bridge_key,
            value=bridge_years * float(m.permanent_annual_spend_nis),
            unit="nis",
            status="resolved",
            source_locator=(
                f"({LUMP_PENSION_AGE} − retirement.fi_age) yrs × "
                "fi_methodology.permanent_annual_spend_nis"
            ),
            agent_report_id=None,
            confidence=conf,
            formula="liquid drawdown to fund permanent-equivalent spend from retirement to the age-60 unlock",
        )
    else:
        values[bridge_key] = ResolvedValue.pending(
            bridge_key, "nis", "needs retirement.fi_age + permanent spend",
        )


# ---------------------------------------------------------------------------
# Synth-prompt rendering — feed the derived headline numbers INTO the
# synthesizer so it consumes them rather than authoring its own.
# ---------------------------------------------------------------------------

# Display order + human labels for the headline numbers the synthesizer is
# allowed to state. Pending keys still render (as [derivation pending]) so the
# model knows the figure exists but has no approved value.
_SYNTH_DISPLAY: tuple[tuple[str, str], ...] = (
    ("portfolio.net_worth_nis", "Liquid net worth (investable; ex-Israel-real-estate)"),
    ("retirement.fi_target_nis", "FI capital target (perpetuity)"),
    ("retirement.fi_total_capital_nis", "FI total capital target (perpetuity + reserve)"),
    ("retirement.fi_margin_signed_nis", "FI sufficiency margin (net worth − total target; >0 => reached)"),
    ("retirement.liquidity_reserve_nis", "Liquidity reserve (finite liabilities, held separately)"),
    ("retirement.fire_bridge_nis", "FIRE bridge (retirement→60 liquid drawdown, permanent-equivalent)"),
    ("concentration.us_situs_estate_exposure_nis", "US-situs estate exposure (IRS NRA — all US-domiciled securities, every broker)"),
    ("spend.fi_basis_nis", "FI spend basis (permanent-equivalent, real)"),
    ("retirement.required_real_yield_pct", "Required real yield (perpetual safe-withdrawal rate)"),
    ("retirement.return_assumption_pct", "Expected real return (trajectory only)"),
    ("retirement.earliest_safe_age", "Earliest safe retirement age — typical drawdown, 90% MC solvency to 95 (THE headline retirement age)"),
    ("retirement.preservation_age", "Capital-preservation retirement age — worst-10% real principal preserved (a what-if, not a constraint)"),
    ("retirement.fi_age", "Full-FI / perpetuity target age — trajectory feasibility, for FIRE-bridge sizing (NOT the earliest-safe age)"),
    ("spend.annual_t12_nis", "Current tracked spend (T12)"),
    ("savings.annual_net_nis", "Annual net savings (RSU, conservative floor)"),
    ("concentration.nvda_cap_pct", "NVDA concentration cap"),
    ("concentration.nvda_current_pct", "NVDA current weight"),
    ("fx.usd_nis", "USD/NIS (BOI daily representative rate)"),
)

PENDING_LABEL = "[derivation pending]"


def _display_value(rv: ResolvedValue) -> str:
    """Render one resolved value for the synth prompt (raw + readable form)."""
    if rv.status != "resolved" or rv.value is None:
        return PENDING_LABEL
    v = float(rv.value)
    if rv.unit == "nis":
        if abs(v) >= 1_000_000:
            return f"₪{v:,.0f} (≈₪{v / 1e6:.2f}M)"
        return f"₪{v:,.0f}"
    if rv.unit == "pct":
        return f"{v * 100:.1f}%"
    if rv.unit == "age":
        return f"age {v:.1f}"
    return f"{v:,.2f}"


def render_numbers_for_synth(resolved: "ResolvedPlanNumbers") -> str:
    """Render the authoritative derived-numbers block for the synth prompt.

    The synthesizer is FORBIDDEN from inventing headline figures; this block
    hands it the deterministically-derived values it MUST consume verbatim,
    and tells it to write ``[derivation pending]`` for any unresolved figure
    instead of guessing (the exact failure that let a stale ₪21M reach a
    draft).
    """
    lines: list[str] = [
        "These are the ONLY approved values for the plan's headline figures. "
        "They are DERIVED deterministically from analyst outputs + a "
        "reviewed methodology and are the single source of truth. You MUST "
        "use these EXACT values for any headline claim (net worth, FI target, "
        "spend, yield, retirement age, savings, NVDA cap/weight). Do NOT round "
        "to a marketing figure, do NOT invent an alternative, and do NOT carry "
        "forward any prior/stale figure from an earlier draft or the baseline "
        "(e.g. a ₪21M FI target). For any line marked "
        f"`{PENDING_LABEL}`, write that literal string instead of a number.",
        "",
    ]
    for key, label in _SYNTH_DISPLAY:
        rv = resolved.get(key)
        disp = _display_value(rv)
        src = rv.source_locator if rv.status == "resolved" else "no approved source"
        conf = f"; conf {rv.confidence}" if rv.confidence else ""
        lines.append(f"  - {label}: {disp}   [{src}{conf}]")
    return "\n".join(lines)


__all__ = [
    "ResolvedValue",
    "ResolvedPlanNumbers",
    "resolve_plan_numbers",
    "render_numbers_for_synth",
    "PENDING_LABEL",
]
