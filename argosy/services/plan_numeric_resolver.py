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
    "retirement.required_real_yield_pct": "pct",
    "retirement.return_assumption_pct": "pct",
    "spend.fi_basis_nis": "nis",
    "savings.annual_net_nis": "nis",
    "spend.annual_t12_nis": "nis",
    "concentration.nvda_cap_pct": "pct",
    "concentration.nvda_current_pct": "pct",
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


def _resolve_net_worth(
    session: "Session", user_id: str
) -> ResolvedValue:
    """Net worth in NIS from the latest portfolio snapshot.

    ``totals_json.total_usd_value_k`` is in THOUSANDS of USD, so
    net_worth_nis = total_usd_value_k * 1000 * fx_usd_nis. Pending when no
    snapshot exists or either factor is non-positive (a guess would be the
    exact fabrication this resolver kills).
    """
    key = "portfolio.net_worth_nis"
    loc = "portfolio_snapshot.totals_json.total_usd_value_k * fx_usd_nis"
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
        return ResolvedValue.pending(key, "nis", loc)
    try:
        totals = json.loads(snap.totals_json or "{}")
    except (json.JSONDecodeError, ValueError, TypeError):
        totals = {}
    total_usd_k = _to_float(totals.get("total_usd_value_k"))
    fx = _to_float(snap.fx_usd_nis)
    if not total_usd_k or not fx or total_usd_k <= 0 or fx <= 0:
        return ResolvedValue.pending(key, "nis", loc)
    return ResolvedValue(
        key=key,
        value=total_usd_k * 1000.0 * fx,
        unit="nis",
        status="resolved",
        source_locator=f"{loc} (snapshot id={snap.id})",
        agent_report_id=None,
        confidence="HIGH",
        formula="total_usd_value_k * 1000 * fx_usd_nis",
    )


def resolve_plan_numbers(
    session: "Session", *, user_id: str, decision_run_id: int
) -> ResolvedPlanNumbers:
    """Resolve all plan headline numbers for one synthesis run.

    Reads the latest portfolio snapshot (net worth) plus the per-role
    ``AgentReport`` rows stamped ``decision_id='plan-synth-<id>'`` and
    parses each through its typed model. Every headline key resolves to a
    :class:`ResolvedValue`; absent / unparseable / missing-field inputs
    resolve to ``status="pending"`` with ``value=None`` — never a guess.

    A parse failure for one role degrades only that role's keys; the
    resolver never raises.
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

    return ResolvedPlanNumbers(values=values)


__all__ = [
    "ResolvedValue",
    "ResolvedPlanNumbers",
    "resolve_plan_numbers",
]
