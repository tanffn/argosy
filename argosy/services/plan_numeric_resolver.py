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
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Callable

from sqlalchemy import select

from argosy.state.models import AgentReport, PlanVersion, PortfolioSnapshotRow

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
    traced to a source; ``"pending"`` when inputs are not yet available;
    ``"excluded"`` when the figure is deliberately out of scope (e.g. NVDA
    weight while NVDA is unmanaged / excluded from sleeve math);
    ``"unavailable"`` when the underlying asset is missing and must not be
    silently reported as zero. Never carries a fabricated constant.
    """

    key: str
    value: float | None
    unit: str
    status: str  # "resolved" | "pending" | "excluded" | "unavailable"
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

    @classmethod
    def excluded(
        cls,
        key: str,
        unit: str,
        source_locator: str,
        *,
        formula: str | None = None,
    ) -> "ResolvedValue":
        """Deliberately out of scope — not zero, not pending."""
        return cls(
            key=key,
            value=None,
            unit=unit,
            status="excluded",
            source_locator=source_locator,
            agent_report_id=None,
            confidence="HIGH",
            formula=formula,
        )

    @classmethod
    def unavailable(
        cls,
        key: str,
        unit: str,
        source_locator: str,
        *,
        formula: str | None = None,
    ) -> "ResolvedValue":
        """Underlying data missing — must not silently resolve to 0.0."""
        return cls(
            key=key,
            value=None,
            unit=unit,
            status="unavailable",
            source_locator=source_locator,
            agent_report_id=None,
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
    "portfolio.liquid_net_worth_nis": "nis",
    "portfolio.total_net_worth_incl_residence_nis": "nis",
    "portfolio.usd_exposure_nis": "nis",
    "retirement.fi_target_nis": "nis",
    "retirement.fi_age": "age",
    "retirement.earliest_safe_age": "age",
    "retirement.preservation_age": "age",
    "retirement.drawdown_scenario_age": "age",
    "retirement.fire_bridge_nis": "nis",
    "retirement.fire_bridge_offmandate_nis": "nis",
    "retirement.fire_bridge_fi_age_estimate_nis": "nis",
    "retirement.required_real_yield_pct": "pct",
    "retirement.return_assumption_pct": "pct",
    "spend.fi_basis_nis": "nis",
    "savings.annual_net_nis": "nis",
    "spend.annual_t12_nis": "nis",
    "concentration.nvda_cap_pct": "pct",
    "concentration.nvda_target_pct": "pct",
    "concentration.nvda_current_pct": "pct",
    "concentration.nvda_value_nis": "nis",
    "concentration.nvda_target_sh": "shares",
    "concentration.nvda_sell_sh": "shares",
    "concentration.nvda_eligible_now_sh": "shares",
    "concentration.nvda_quota_tax_year_sh": "shares",
    "retirement.liquidity_reserve_nis": "nis",
    "retirement.fi_total_capital_nis": "nis",
    "retirement.fi_margin_signed_nis": "nis",
    "retirement.fi_shock_net_worth_nis": "nis",
    "retirement.fi_fx_shock_net_worth_nis": "nis",
    "retirement.fi_crossing_year": "year",
    "retirement.pension_unlock_age": "age",
    "retirement.mc_horizon_age": "age",
    "tax.retention_at_vest_pct": "pct",
    "tax.retention_capital_track_pct": "pct",
    # Net-of-realization keys — derived from the tax-simulation report
    "tax.nvda_embedded_cgt_nis": "nis",
    "tax.nvda_net_proceeds_nis": "nis",
    "retirement.fi_margin_net_of_realization_nis": "nis",
    # Glide-consistent counterparts (tax on the PLANNED sale only, not full
    # liquidation) — added alongside the full-liquidation pair above, never
    # replacing it. See _apply_nvda_realization_tax_glide.
    "tax.nvda_embedded_cgt_glide_nis": "nis",
    "retirement.fi_margin_net_of_realization_glide_nis": "nis",
    # DATED counterparts (RED-9) — eligibility projected forward per-lot via the
    # Section-102 24-months-from-grant clock instead of frozen at the report's
    # point-in-time markings. See _apply_nvda_realization_tax_glide_dated.
    "tax.nvda_embedded_cgt_glide_dated_nis": "nis",
    "retirement.fi_margin_net_of_realization_glide_dated_nis": "nis",
    "concentration.nvda_eligible_by_glide_horizon_sh": "shares",
}

# Fixed STRUCTURAL ages — not derived, not MC-dependent. The pension unlock age
# (Israeli keren-hishtalmut / kupat-gemel lump availability + the FIRE-bridge
# endpoint, see retirement/derived_inputs.py) and the Monte-Carlo solvency
# horizon (every drawdown MC runs P(ruin) to this age). Registering them as
# resolved facts lets the synthesizer placeholder them ({{fact:}}) instead of
# hand-typing 60 / 95 — so a correctly-stated age stops tripping the
# headline_numeric_source gate. Single-sourced here so a change is one edit.
PENSION_UNLOCK_AGE = 60.0
MC_HORIZON_AGE = 95.0

# Israeli Section-102 capital-track HIGH-INCOME marginal effective rate on the
# capital-gain slice: 25% base CGT + 3% general surtax + 2% capital-source surtax
# (applies once capital-source income exceeds the threshold). domain_knowledge/
# tax/israel/section_102.md: "use 30% marginal effective" for the post-24-month
# NVDA tranche. Statutory policy parameter, NOT a guess (codex tax review 2026-06-20).
SECTION_102_HIGH_INCOME_RATE = 0.30
# At-vest ORDINARY-income high-income effective rate: 47% top marginal + 3% general
# surtax = 50% (domain_knowledge/tax/israel/surtax.md). Statutory policy parameter.
ORDINARY_HIGH_INCOME_RATE = 0.50


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


def _head_snapshot_row(session: "Session", user_id: str):
    """Canonical current-book head pick for EVERY money resolver.

    (imported_at DESC, id DESC) via the ONE store accessor. A bare
    ``id.desc()`` diverged from /portfolio + the dashboard + the repair
    script — a backfilled/restore row with a higher id but older import could
    surface a different book than the plan (Sol BLOCK-6/#4). Imported lazily to
    avoid a module import cycle with portfolio_snapshot_store.
    """
    from argosy.services.portfolio_snapshot_store import get_latest_snapshot_row
    return get_latest_snapshot_row(session, user_id)


def _spine_gate_refuses(session: "Session", user_id: str, snap: Any) -> bool:
    """SPINE GATE (Phase 3c) for the resolver's DIRECT head loads.

    These money resolvers read the head snapshot straight from the store and
    call ``load_total_book`` — they BYPASS ``load_current_book`` and so never
    saw the ``CurrentBook.validated`` flag. Consult the shared predicate here.

    Returns ``True`` only when enforcement is ON (``spine_gate_enforce``, default
    OFF) AND the head snapshot is NOT validated — the caller then degrades to its
    existing ``unavailable`` shape. In the DEFAULT (warn) config this ALWAYS
    returns ``False`` (dormant) — zero behavior change.
    """
    try:
        from argosy.config import get_settings

        if not get_settings().spine_gate_enforce:
            return False
    except Exception:  # noqa: BLE001 — config read must not break resolution
        return False
    from argosy.services.spine.validated_snapshot import is_snapshot_validated

    if is_snapshot_validated(session, user_id=user_id, snapshot=snap):
        return False
    log.warning(
        "plan_numeric_resolver.spine_gate_refuse snapshot_id=%s",
        getattr(snap, "id", None),
    )
    return True


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

    out_values = [
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

    # NOTE: the at-vest ordinary retention is NOT sourced from this analyst's
    # net_retention_pct — on live run 117 that field read 72% (a blended/after-sale
    # figure), which contradicts the at-vest ORDINARY rate (~50%). The two
    # statutory retention rates are published deterministically in
    # _apply_retention_rates (auditable to domain_knowledge/tax/israel), not from
    # this ambiguous field.
    return out_values


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
        snap = _head_snapshot_row(session, user_id)
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
    # TOTAL book — merge durable unmanaged holdings (NVDA) so a TSV that
    # omitted Schwab cannot understate net worth. Fail loud when degraded.
    from argosy.services.holding_books import (
        load_total_book,
        parse_positions_json,
    )

    usd_assets_usd = 0.0
    nis_native_nis = 0.0
    try:
        raw_positions = parse_positions_json(snap.positions_json)
    except Exception:  # noqa: BLE001
        raw_positions = []
    book = load_total_book(
        session, user_id, raw_positions,
        snapshot_date=getattr(snap, "snapshot_date", None),
        # Real current date, never the snapshot's own — see Sol BLOCK-1.
    )
    if book.degraded:
        return ResolvedValue.unavailable(
            key, "nis",
            f"portfolio_snapshot DEGRADED: {book.degrade_reason}",
            formula=(
                "unavailable: refusing understated net worth at HIGH confidence "
                "when unmanaged book cannot restore a policy holding"
            ),
        )
    if _spine_gate_refuses(session, user_id, snap):
        return ResolvedValue.unavailable(
            key, "nis", "spine gate: head snapshot not validated (no PASS verdict)",
            formula="unavailable: spine_gate_enforce ON and book not validated",
        )
    positions = book.total
    # A soft-stale mark in the book means net worth isn't fully current money —
    # downgrade from HIGH rather than republish stale as HIGH (Sol BLOCK-1).
    _nw_conf = "MEDIUM" if book.stale_marks else "HIGH"
    _nw_stale_note = (
        f" [STALE MARK — soft-stale marks in book "
        f"({', '.join(book.stale_marks)}); confidence downgraded from HIGH]"
        if book.stale_marks
        else ""
    )
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
        source_locator=loc + _nw_stale_note, agent_report_id=None,
        confidence=_nw_conf, formula=formula,
    )


def _apply_total_net_worth(session, user_id, values):
    """Register total net worth INCL. primary-residence equity — the third
    canonical basis (alongside investable portfolio.net_worth_nis and liquid
    portfolio.liquid_net_worth_nis). Single-sourced from the shared
    net_worth_bases helper the Wealth Dashboard also uses, so the two cannot
    diverge. Pending (never a guess) when no snapshot/FX exists."""
    from argosy.services.net_worth_bases import total_net_worth_incl_residence
    key = "portfolio.total_net_worth_incl_residence_nis"
    try:
        snap = _head_snapshot_row(session, user_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("plan_numeric_resolver.total_net_worth_query_failed err=%s", exc)
        snap = None
    if snap is None:
        values[key] = ResolvedValue.pending(key, "nis", "portfolio_snapshot (none)")
        return
    if _spine_gate_refuses(session, user_id, snap):
        values[key] = ResolvedValue.unavailable(
            key, "nis", "spine gate: head snapshot not validated (no PASS verdict)",
        )
        return
    snap_fx = _to_float(snap.fx_usd_nis) or 0.0
    fx, _src = _current_boi_usd_nis(session, snap_fx)
    if not fx or fx <= 0:
        values[key] = ResolvedValue.pending(key, "nis", "no FX available")
        return
    try:
        nw_nis, _ = total_net_worth_incl_residence(
            snapshot=snap, fx_usd_nis=fx, session=session, user_id=user_id)
    except Exception as exc:  # noqa: BLE001 — never break the resolver
        log.warning("plan_numeric_resolver.total_net_worth_failed err=%s", exc)
        nw_nis = None
    if nw_nis is None:
        values[key] = ResolvedValue.pending(key, "nis", "total net worth unavailable")
        return
    # Downgrade from HIGH when the book carries a soft-stale mark (Sol round-5
    # #5: total NW hard-coded HIGH regardless of staleness).
    from argosy.services.current_book import load_current_book
    _cb = load_current_book(session, user_id)
    _tnw_conf = "MEDIUM" if _cb.stale_marks else "HIGH"
    values[key] = ResolvedValue(
        key=key, value=float(nw_nis), unit="nis", status="resolved",
        source_locator=(
            "net_worth_bases.total_net_worth_incl_residence" + _cb.stale_note()
        ),
        confidence=_tnw_conf,
        formula="investable net worth + real-estate net equity (incl. primary residence)")


def _is_real_estate(position: dict) -> bool:
    """True when a position is ILLIQUID / direct real estate that must be
    EXCLUDED from the liquid FI sufficiency basis.

    Driven by the snapshot's own ``asset_type`` / ``details`` fields — never a
    hardcoded value. A LISTED property security (a property ETF / REIT with a
    tradable ticker, e.g. ``IWDP``, ``O``) is liquid spendable capital and stays
    IN the liquid basis; only direct/illiquid holdings (foreign property carrying
    no tradable ticker — the snapshot tags these ``symbol='-'``) are excluded.

    This closes a keyword-classifier asymmetry (codex 2026-06-19): the prior
    ``"real estate" in blob`` test dropped ``IWDP`` ("Real Estate") from liquid
    while keeping ``O`` ("REIT") — same economic class, opposite treatment, by
    accident of the keyword. The FI policy is "exclude illiquid property," not
    "exclude every row whose label mentions real estate."
    """
    blob = " ".join(
        str(position.get(k) or "") for k in ("asset_type", "details", "category", "type")
    ).lower()
    if "real estate" not in blob and "real-estate" not in blob:
        return False
    # A listed property security has a tradable ticker → liquid (keep it). Only a
    # real-estate-tagged row WITHOUT a tradable ticker is direct/illiquid property.
    sym = str(position.get("symbol") or "").strip().lower()
    has_tradable_ticker = bool(sym) and sym not in {"-", "—", "n/a", "na", "none"}
    return not has_tradable_ticker


def liquid_components_from_positions(
    positions: list, *, fx: float, snap_fx: float
) -> tuple[float, float, float]:
    """Split snapshot positions into the LIQUID basis components, EXCLUDING
    real-estate rows: ``(usd_assets_usd, nis_native_nis, re_excluded_nis)``.

    This is the SINGLE source the resolver's ``portfolio.liquid_net_worth_nis``
    and the plan render's FX-risk block both bind to, so the two surfaces cannot
    diverge on whether real estate counts toward the FI sufficiency basis. The
    bug it closes: the render summed positions INCLUDING the foreign-property
    rows (₪11,954,153), computing a +₪118,020 FI *surplus* that contradicted the
    canonical liquid basis ₪11,687,926 (−₪148,208 *short*) — a sign-flipped FI
    verdict the whole-artifact reader (correctly) BLOCKED on.
    """
    usd_assets_usd = 0.0
    nis_native_nis = 0.0
    re_excluded_nis = 0.0
    eff_nis_fx = snap_fx if snap_fx > 0 else fx
    for p in positions:
        v = _to_float(p.get("usd_value_k")) or 0.0
        is_usd = (p.get("currency") or "").upper() == "USD"
        if _is_real_estate(p):
            re_excluded_nis += v * 1000.0 * (fx if is_usd else eff_nis_fx)
            continue
        if is_usd:
            usd_assets_usd += v * 1000.0
        else:
            nis_native_nis += v * 1000.0 * eff_nis_fx
    return usd_assets_usd, nis_native_nis, re_excluded_nis


def _resolve_liquid_net_worth(
    session: "Session", user_id: str
) -> ResolvedValue:
    """Liquid/investable net worth — total net worth EXCLUDING real-estate
    positions (the snapshot tags them ``asset_type='Real estate'``).

    The plan compares FI capital sufficiency on a liquid/investable basis, so
    counting an illiquid foreign-property row inside "liquid net worth" overstates
    sufficiency (codex/reader 2026-06-17). This is the honest liquid figure shown
    ALONGSIDE the real-estate-inclusive ``portfolio.net_worth_nis`` ("show both").
    Pending — never fabricated — when no snapshot exists.
    """
    key = "portfolio.liquid_net_worth_nis"
    try:
        snap = _head_snapshot_row(session, user_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("plan_numeric_resolver.liquid_nw_query_failed err=%s", exc)
        snap = None
    if snap is None:
        return ResolvedValue.pending(key, "nis", "portfolio_snapshot (none)")

    snap_fx = _to_float(snap.fx_usd_nis) or 0.0
    fx, fx_src = _current_boi_usd_nis(session, snap_fx)
    if not fx or fx <= 0:
        return ResolvedValue.pending(key, "nis", "no FX available")

    # Go through the book loader (real today) so a stale/degraded book cannot
    # publish liquid net worth as HIGH-confidence current money (Sol BLOCK-1);
    # book.total also includes durable unmanaged holdings the raw snapshot may
    # omit (the understatement guard net worth already uses).
    from argosy.services.holding_books import load_total_book, parse_positions_json
    _book = load_total_book(
        session, user_id, parse_positions_json(snap.positions_json),
        snapshot_date=getattr(snap, "snapshot_date", None),
    )
    if _book.degraded:
        return ResolvedValue.unavailable(
            key, "nis", f"portfolio_snapshot DEGRADED: {_book.degrade_reason}",
        )
    if _spine_gate_refuses(session, user_id, snap):
        return ResolvedValue.unavailable(
            key, "nis", "spine gate: head snapshot not validated (no PASS verdict)",
        )
    usd_assets_usd, nis_native_nis, re_excluded_nis = liquid_components_from_positions(
        _book.total, fx=fx, snap_fx=snap_fx,
    )

    if usd_assets_usd <= 0 and nis_native_nis <= 0:
        return ResolvedValue.pending(key, "nis", "snapshot has no liquid positions")
    value = usd_assets_usd * fx + nis_native_nis
    holdings_as_of = getattr(snap, "snapshot_date", None)
    as_of = holdings_as_of.isoformat() if holdings_as_of else f"snapshot id={snap.id}"
    loc = (
        f"liquid = USD ${usd_assets_usd/1e6:.2f}M × {fx_src} {fx:.3f} + NIS-native "
        f"₪{nis_native_nis:,.0f}, EXCLUDING ₪{re_excluded_nis:,.0f} real estate; "
        f"holdings as of {as_of} (provisional)"
    )
    _liq_conf = "MEDIUM" if _book.stale_marks else "HIGH"
    _liq_note = (
        " [STALE MARK — soft-stale marks in book; confidence downgraded from HIGH]"
        if _book.stale_marks
        else ""
    )
    return ResolvedValue(
        key=key, value=value, unit="nis", status="resolved",
        source_locator=loc + _liq_note, agent_report_id=None, confidence=_liq_conf,
        formula="net worth EXCLUDING asset_type='Real estate' positions",
    )


def _resolve_usd_exposure(
    session: "Session", user_id: str
) -> ResolvedValue:
    """NIS value of USD-DENOMINATED assets — the FX-sensitive base.

    This is the gross USD exposure used by the FI-sufficiency-under-FX-shock gate
    (codex FX-shock review 2026-06-16): a −10% USD/NIS move marks this sleeve
    down, so the right base is *all* USD-denominated assets, NOT the US-situs
    estate-exposure figure (which excludes USD cash + Irish USD UCITS and so
    understates FX exposure — the unsafe direction for a fail-loud gate).

    Mirrors :func:`_resolve_net_worth`'s snapshot + position read: sums
    ``usd_value_k`` for positions whose ``currency == "USD"`` and converts at the
    current BOI rate; falls back to ``total_usd_value_k`` (all USD-denominated)
    when per-position currencies are absent. Pending — never fabricated — when no
    snapshot / no USD figure exists.
    """
    key = "portfolio.usd_exposure_nis"
    try:
        snap = _head_snapshot_row(session, user_id)
    except Exception as exc:  # noqa: BLE001 — defensive
        log.warning("plan_numeric_resolver.usd_exposure_snapshot_query_failed err=%s", exc)
        snap = None
    if snap is None:
        return ResolvedValue.pending(key, "nis", "portfolio_snapshot (none)")

    snap_fx = _to_float(snap.fx_usd_nis) or 0.0
    fx, fx_src = _current_boi_usd_nis(session, snap_fx)
    if not fx or fx <= 0:
        return ResolvedValue.pending(key, "nis", "no FX available")

    # Real today via the book loader — a stale/degraded book must not publish
    # USD exposure as HIGH (Sol BLOCK-1); book.total includes durable unmanaged
    # holdings (NVDA is USD) the raw snapshot may omit.
    from argosy.services.holding_books import load_total_book, parse_positions_json
    _book = load_total_book(
        session, user_id, parse_positions_json(snap.positions_json),
        snapshot_date=getattr(snap, "snapshot_date", None),
    )
    if _book.degraded:
        return ResolvedValue.unavailable(
            key, "nis", f"portfolio_snapshot DEGRADED: {_book.degrade_reason}",
        )
    if _spine_gate_refuses(session, user_id, snap):
        return ResolvedValue.unavailable(
            key, "nis", "spine gate: head snapshot not validated (no PASS verdict)",
        )
    _usd_conf = "MEDIUM" if _book.stale_marks else "HIGH"
    _usd_note = (
        " [STALE MARK — soft-stale marks in book; confidence downgraded from HIGH]"
        if _book.stale_marks
        else ""
    )
    usd_assets_usd = 0.0
    saw_currency = False
    for p in _book.total:
        if (p.get("currency") or "").upper() == "USD":
            saw_currency = True
            usd_assets_usd += (_to_float(p.get("usd_value_k")) or 0.0) * 1000.0

    holdings_as_of = getattr(snap, "snapshot_date", None)
    as_of = holdings_as_of.isoformat() if holdings_as_of else f"snapshot id={snap.id}"
    if saw_currency and usd_assets_usd > 0:
        value = usd_assets_usd * fx
        loc = (
            f"USD-denominated assets ${usd_assets_usd/1e6:.2f}M × {fx_src} {fx:.3f}; "
            f"holdings as of {as_of} (provisional)"
        )
        formula = "sum(positions where currency=USD).usd_value_k * 1000 * current BOI USD/NIS"
    else:
        # No per-position currencies → fall back to the snapshot's total USD book
        # (all USD-denominated) × current FX.
        try:
            totals = json.loads(snap.totals_json or "{}")
        except (json.JSONDecodeError, ValueError, TypeError):
            totals = {}
        total_usd_k = _to_float(totals.get("total_usd_value_k"))
        if not total_usd_k or total_usd_k <= 0:
            return ResolvedValue.pending(key, "nis", "snapshot has no USD positions/totals")
        value = total_usd_k * 1000.0 * fx
        loc = f"total_usd_value_k ${total_usd_k/1e3:.2f}M × {fx_src} {fx:.3f}; holdings as of {as_of} (provisional)"
        formula = "total_usd_value_k * 1000 * current BOI USD/NIS"

    return ResolvedValue(
        key=key, value=value, unit="nis", status="resolved",
        source_locator=loc + _usd_note, agent_report_id=None,
        confidence=_usd_conf, formula=formula,
    )


def _phase_reuse_donor_chain(
    session: "Session", decision_run_id: int, *, max_hops: int = 3
) -> list[int]:
    """Follow the corrective phase-reuse lineage of ``decision_run_id``.

    A corrective re-synthesis run reuses another run's persisted phase 1-2
    outputs (analysts + debates) and does NOT re-run them, so its own
    ``plan-synth-<id>`` token has no phase-1 ``agent_reports`` rows. The donor
    is recorded in two places, checked in order:

      1. ``decision_runs.notes_json['phase_reuse_from_run_id']`` — stamped by
         the orchestrator the moment reuse is decided, so IN-RUN resolver
         calls (scrub / appendix render, which happen before the draft row
         persists) can already follow the lineage.
      2. ``plan_versions.synthesis_inputs_json['corrective']['reused_from_run_id']``
         — persisted with the draft; covers post-hoc resolution for runs that
         predate the notes_json stamp.

    Returns donor run ids in hop order (nearest first). Cycle-guarded and
    bounded by ``max_hops``. Best-effort: any error returns what was found.
    """
    chain: list[int] = []
    seen: set[int] = set()
    current = decision_run_id
    try:
        current = int(current)
    except (TypeError, ValueError):
        return chain
    seen.add(current)
    from sqlalchemy import text as _sa_text

    for _ in range(max_hops):
        donor: int | None = None
        try:
            notes_raw = session.execute(
                _sa_text("select notes_json from decision_runs where id = :i"),
                {"i": current},
            ).scalar()
            if notes_raw:
                notes = json.loads(notes_raw)
                if isinstance(notes, dict):
                    d = notes.get("phase_reuse_from_run_id")
                    donor = int(d) if d is not None else None
        except Exception as exc:  # noqa: BLE001 — lineage lookup is best-effort
            log.warning(
                "plan_numeric_resolver.reuse_lineage_notes_failed run=%s err=%s",
                current, exc,
            )
        if donor is None:
            try:
                sij_raw = session.execute(
                    _sa_text(
                        "select synthesis_inputs_json from plan_versions "
                        "where decision_run_id = :i "
                        "order by id desc limit 1"
                    ),
                    {"i": current},
                ).scalar()
                if sij_raw:
                    sij = json.loads(sij_raw)
                    corr = sij.get("corrective") if isinstance(sij, dict) else None
                    if isinstance(corr, dict):
                        d = corr.get("reused_from_run_id")
                        donor = int(d) if d is not None else None
            except Exception as exc:  # noqa: BLE001 — lineage lookup is best-effort
                log.warning(
                    "plan_numeric_resolver.reuse_lineage_inputs_failed run=%s err=%s",
                    current, exc,
                )
        if donor is None or donor in seen:
            break
        chain.append(donor)
        seen.add(donor)
        current = donor
    return chain


def find_report_donor_run_id(
    session: "Session", *, user_id: str, plan_version: Any, max_hops: int = 10,
) -> int | None:
    """Find the nearest decision run — starting at ``plan_version`` itself,
    then walking ``derived_from_id`` ancestry — that actually persisted
    phase-1 ``agent_reports`` (a real full-synthesis product), and return
    its id, or ``None`` if none in range has any.

    Written for the RED-15 pending-donor gap: a medium AMENDMENT worker
    (``plan_amendment.workers._medium_worker``) stamps its OWN new
    ``decision_run_id`` on the draft it persists (Phase 3 only — analysts
    never re-run), so ``state.queries.nearest_ancestor_decision_run_id``
    (which trusts ANY non-null ``decision_run_id`` as "a real synthesis
    product") walks straight past the amendment run without noticing it
    has zero ``agent_reports``. This verifies presence via an actual query
    instead of trusting the column being set. Tenant-scoped: stops if the
    walk crosses into a plan_version owned by a different user (should
    never happen for a same-user derived_from_id chain, but is not
    something to silently paper over).
    """
    from argosy.state.models import PlanVersion as _PV

    def _has_reports(run_id: int) -> bool:
        return session.execute(
            select(AgentReport.id)
            .where(AgentReport.decision_id == f"plan-synth-{run_id}")
            .limit(1)
        ).scalar_one_or_none() is not None

    seen_plan_ids: set[int] = set()
    seen_run_ids: set[int] = set()
    current = plan_version
    for _ in range(max_hops + 1):
        if current is None:
            return None
        if getattr(current, "user_id", user_id) != user_id:
            log.warning(
                "plan_numeric_resolver.donor_walk_cross_tenant plan_id=%s",
                getattr(current, "id", None),
            )
            return None
        pid = getattr(current, "id", None)
        if pid is not None:
            if pid in seen_plan_ids:
                return None
            seen_plan_ids.add(pid)
        run_id = getattr(current, "decision_run_id", None)
        if run_id is not None and run_id not in seen_run_ids:
            seen_run_ids.add(run_id)
            try:
                if _has_reports(run_id):
                    return run_id
            except Exception as exc:  # noqa: BLE001 — best-effort lookup
                log.warning(
                    "plan_numeric_resolver.donor_walk_query_failed run=%s err=%s",
                    run_id, exc,
                )
        parent_id = getattr(current, "derived_from_id", None)
        if not parent_id:
            return None
        current = session.get(_PV, parent_id)
    return None


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

    # Third canonical basis: total net worth INCL. primary-residence equity,
    # single-sourced from the shared net_worth_bases helper the dashboard uses.
    _apply_total_net_worth(session, user_id, values)

    # Snapshot-derived USD exposure (the FX-shock base — codex FX review).
    usd_exp = _resolve_usd_exposure(session, user_id)
    values[usd_exp.key] = usd_exp

    # Liquid/investable net worth (ex real estate) — shown alongside the
    # real-estate-inclusive net worth for honest FI-sufficiency framing.
    liquid_nw = _resolve_liquid_net_worth(session, user_id)
    values[liquid_nw.key] = liquid_nw

    decision_id = f"plan-synth-{decision_run_id}"

    # Corrective re-synthesis lineage (docs/design/corrective_resynthesis.md
    # §2.B.3): a corrective run reuses ANOTHER completed run's phase 1-2 outputs
    # and never re-runs the analysts, so its own ``plan-synth-<id>`` audit token
    # has NO phase-1 agent_reports. Without following that lineage every
    # agent-sourced key (withdrawal_sequencer → fi_age; equity_comp →
    # savings.annual_net_nis; household_budget → spend.annual_t12_nis;
    # concentration caps) degrades to pending and the draft renders
    # ``[derivation pending]`` — the run-156 / draft-73 leak. Resolve the donor
    # chain up front so each role can fall back to the run whose analyst
    # outputs this synthesis actually consumed.
    donor_decision_ids = [
        f"plan-synth-{d}"
        for d in _phase_reuse_donor_chain(session, decision_run_id)
    ]

    # RED-15 runtime fallback: a run whose notes_json/synthesis_inputs_json
    # was never stamped with a donor (either it predates the worker's donor
    # write, or the write only happens going forward) still needs a chance
    # to inherit — the amendment's OWN plan_versions row (draft/current with
    # this decision_run_id) is right here in the DB with a real
    # derived_from_id ancestry, so compute the donor on the fly rather than
    # resolving pending merely because nobody pre-recorded the lineage. This
    # is what makes existing amendment runs (already completed before this
    # fix shipped) resolve too, not just new ones.
    if not donor_decision_ids:
        _fallback_donor: int | None = None
        try:
            _amend_plan = session.execute(
                select(PlanVersion)
                .where(PlanVersion.decision_run_id == decision_run_id)
                .order_by(PlanVersion.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            if _amend_plan is not None:
                _chain_donor = find_report_donor_run_id(
                    session, user_id=user_id, plan_version=_amend_plan,
                )
                # find_report_donor_run_id also matches the run's OWN id
                # when it itself has reports — only a genuinely DIFFERENT
                # run counts as a donor.
                if _chain_donor is not None and _chain_donor != decision_run_id:
                    _fallback_donor = _chain_donor
        except Exception as exc:  # noqa: BLE001 — best-effort, never break
            log.warning(
                "plan_numeric_resolver.runtime_donor_fallback_failed run=%s err=%s",
                decision_run_id, exc,
            )

        # A medium AMENDMENT worker sets ``derived_from_id`` to the ACTIVE
        # BASELINE (the originally-imported plan, which has no
        # decision_run_id at all — see plan_amendment.workers._medium_worker),
        # not to the immediately-preceding draft. So for amendment-produced
        # plans the plan-lineage walk above almost always dead-ends at the
        # baseline with nothing found, even though a perfectly good recent
        # full-synthesis run exists. Last resort: the most recent run (this
        # user, any plan-lineage) that actually persisted phase-1
        # ``agent_reports`` — never fabricated, always the REAL latest
        # analyst output on file, and logged explicitly so this is never a
        # silent inheritance.
        if _fallback_donor is None:
            try:
                _latest_report = session.execute(
                    select(AgentReport.decision_id)
                    .where(AgentReport.user_id == user_id)
                    .where(AgentReport.decision_id.like("plan-synth-%"))
                    .where(AgentReport.decision_id != decision_id)
                    .order_by(AgentReport.id.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if _latest_report:
                    try:
                        _fallback_donor = int(_latest_report.rsplit("-", 1)[-1])
                    except (ValueError, IndexError):
                        _fallback_donor = None
                    if _fallback_donor == decision_run_id:
                        _fallback_donor = None
            except Exception as exc:  # noqa: BLE001 — best-effort, never break
                log.warning(
                    "plan_numeric_resolver.latest_report_donor_failed run=%s err=%s",
                    decision_run_id, exc,
                )

        if _fallback_donor is not None:
            log.info(
                "plan_numeric_resolver.runtime_donor_fallback run=%s donor=%s",
                decision_run_id, _fallback_donor,
            )
            donor_decision_ids = [f"plan-synth-{_fallback_donor}"]

    for role, (keys, fn) in _RESOLVERS.items():
        # Latest report for this role within the run (highest id wins);
        # falls back through the corrective phase-reuse donor chain.
        report = None
        for cand_decision_id in [decision_id, *donor_decision_ids]:
            try:
                report = session.execute(
                    select(AgentReport)
                    .where(AgentReport.decision_id == cand_decision_id)
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
            if report is not None:
                if cand_decision_id != decision_id:
                    log.info(
                        "plan_numeric_resolver.report_from_reused_run "
                        "role=%s run=%s donor=%s report_id=%s",
                        role, decision_id, cand_decision_id, report.id,
                    )
                break

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
        # Non-negotiable provenance rule: a value pulled from a donor run's
        # agent_reports (this run had none of its own for this role) must
        # NEVER be indistinguishable from a value this run actually derived.
        # Stamp the donor run id into source_locator so no surface can claim
        # the amendment itself computed it.
        if cand_decision_id != decision_id:
            _donor_run_id = cand_decision_id.rsplit("-", 1)[-1]
            resolved = [
                replace(
                    rv,
                    source_locator=f"{rv.source_locator} (inherited from donor run {_donor_run_id})",
                )
                if rv.status == "resolved"
                else rv
                for rv in resolved
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
    _apply_structural_ages(values)
    _apply_retention_rates(values)
    # B1 NOTE: a deterministic contractual-RSU calculator exists
    # (argosy.services.rsu_savings.contractual_rsu_net_by_year) and is the building
    # block for the savings fix. It is deliberately NOT wired to override the flat
    # `savings.annual_net_nis` scalar: codex review found the contractual grants run
    # off steeply (2026 ₪519k → 2030 ₪18k), so a flat 5-yr MEAN understates exactly the
    # 2026-2028 window where the FI crossing falls — the correct fix is feeding the
    # PER-YEAR vector into the trajectory + fi_crossing (render.fv/years_to +
    # _apply_fi_crossing_year), a headline-figure change tracked as the B1 follow-up.

    # ONE signed FI sufficiency margin (net_worth − FI-total-capital). Computed
    # once here so every surface cites the SAME signed number — the
    # reached/not-reached sign can never diverge across surfaces again.
    _apply_fi_margin(values)

    # Shocked net-worth figures the promote gate already computes — publish so
    # prose can cite them without [derivation pending] (draft 81 FM rejection).
    _apply_fi_shock_net_worths(values)

    # ONE canonical FI-crossing year, derived from the resolver's own figures and
    # reconciled with the FI-margin verdict by construction (margin >= 0 => current
    # year; margin < 0 => strictly future). Called AFTER _apply_fi_margin so the
    # margin + all inputs are resolved.
    _apply_fi_crossing_year(values)

    # DERIVED NVDA deconcentration (target/sell + capital-track-eligible count) as
    # authoritative values, so the synthesizer uses a DERIVED target instead of
    # inheriting the baseline doc's sale cadence (the 3,000 class).
    _apply_nvda_deconcentration(session, user_id, values)

    # The ADJUDICATED NVDA glide/vest policy — read from the settled proposal
    # substrate (the fleet-authored, user-accepted glide-schedule verdict).
    # Determinism STATES the settled policy; it never authors one.
    _apply_adjudicated_glide(session, user_id, values)

    # Embedded NVDA realization tax — derived deterministically from the tax-simulation
    # table.  Called AFTER _apply_nvda_deconcentration (so the NVDA price is already
    # loaded into the book) and after _apply_fx_boi (FX is resolved).
    _apply_nvda_realization_tax(session, user_id, values)

    # Glide-consistent counterpart: tax on the PLANNED sale only (nvda_sell_sh,
    # eligible/breaking split respected) — never blurs the two rates, never
    # invents a per-year sale schedule (single-year, stated as such). Called
    # after _apply_nvda_deconcentration (sell/eligible counts) and the full
    # realization-tax pass (shares FX/price sourcing).
    _apply_nvda_realization_tax_glide(session, user_id, values)

    # DATED counterpart (RED-9): eligibility projected forward per-lot to the
    # settled glide horizon instead of frozen at the report's point-in-time
    # markings. Alongside, never replacing, the single-year figure above.
    _apply_nvda_realization_tax_glide_dated(session, user_id, values)

    # FI margin net of the embedded NVDA realization tax.  Adds the honest after-tax
    # FI sufficiency figure alongside the gross margin — never replaces it.
    _apply_fi_margin_net_of_realization(values)

    # Glide-consistent net margin (plan-as-written), alongside the full-liquidation
    # bound above — FM run 379 required BOTH visible, neither replacing the other.
    _apply_fi_margin_net_of_realization_glide(values)

    # DATED counterpart of the glide-consistent net margin (RED-9).
    _apply_fi_margin_net_of_realization_glide_dated(values)

    # Canonical dual-track retirement ages — DISPLAY surfaces only (see the
    # docstring re: re-entrancy + MC cost). Gated so the re-entrant NVDA-haircut
    # hop and the non-display callers never trigger the heavy canonical MC.
    if include_canonical_ages:
        _apply_canonical_dual_track_age(session, user_id, values)
        _apply_canonical_mc_spend(session, user_id, values)
        if decision_run_id is not None:
            _apply_canonical_allocation(session, decision_run_id, values)

    return ResolvedPlanNumbers(values=values)


def _apply_structural_ages(values: dict[str, ResolvedValue]) -> None:
    """Register the two FIXED structural ages as resolved facts. These are
    constants (no DB read, no MC), so they resolve unconditionally — present on
    BOTH the default and the canonical-age resolver paths. The synthesizer can
    then placeholder them instead of typing 60 / 95, removing them as a
    headline_numeric_source false-positive source."""
    values["retirement.pension_unlock_age"] = ResolvedValue(
        key="retirement.pension_unlock_age", value=PENSION_UNLOCK_AGE, unit="age",
        status="resolved", source_locator="plan_numeric_resolver.PENSION_UNLOCK_AGE",
        confidence="HIGH",
        formula="Israeli pension/hishtalmut lump-availability age (FIRE-bridge endpoint)",
    )
    values["retirement.mc_horizon_age"] = ResolvedValue(
        key="retirement.mc_horizon_age", value=MC_HORIZON_AGE, unit="age",
        status="resolved", source_locator="plan_numeric_resolver.MC_HORIZON_AGE",
        confidence="HIGH",
        formula="Monte-Carlo solvency horizon (every drawdown P(ruin) runs to this age)",
    )


def _apply_retention_rates(values):
    """Publish BOTH RSU net-retention rates as DISTINCT, statutory-derived figures
    so prose can never conflate them (the recurring reader contradiction):

      * tax.retention_at_vest_pct — retention on AT-VEST ORDINARY income: top
        marginal IL 47% + 3% general surtax = 50% tax (domain_knowledge/tax/israel/
        surtax.md: ordinary income above the threshold). retention = 0.50.
      * tax.retention_capital_track_pct — retention on the Section-102 capital-GAIN
        SLICE at the high-income marginal: 25% CGT + 3% + 2% capital-source surtax
        = 30% (section_102.md "use 30% marginal effective"). retention = 0.70.

    Both are statutory policy parameters auditable to domain knowledge — not the
    equity_comp analyst's ambiguous blended net_retention_pct (72% on run 117)."""
    values["tax.retention_at_vest_pct"] = ResolvedValue(
        key="tax.retention_at_vest_pct", value=1.0 - ORDINARY_HIGH_INCOME_RATE,
        unit="pct", status="resolved",
        source_locator="plan_numeric_resolver.ORDINARY_HIGH_INCOME_RATE (domain_knowledge/tax/israel/surtax.md)",
        confidence="HIGH",
        formula="1 - at-vest ordinary high-income rate (47% marginal + 3% surtax)")
    values["tax.retention_capital_track_pct"] = ResolvedValue(
        key="tax.retention_capital_track_pct", value=1.0 - SECTION_102_HIGH_INCOME_RATE,
        unit="pct", status="resolved",
        source_locator="plan_numeric_resolver.SECTION_102_HIGH_INCOME_RATE (domain_knowledge/tax/israel/section_102.md)",
        confidence="HIGH",
        formula="1 - Section-102 high-income marginal (25% CGT + 3% + 2% surtax) on the capital-gain slice")


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
    """Resolve the CANONICAL dual-track retirement ages from
    ``retirement_plan.canonical_feasible_dual_track`` (the same basis the
    /retirement panel, ruin hero, and scenario grid bind to).

    ARIEL'S RULING (2026-08-18, plan-109 review RED-16) — publish BOTH ages,
    NO single headline:

      * ``retirement.preservation_age`` — the MANDATE-SATISFYING reading:
        worst-10% real path preserves today's real principal to 95, >=99%
        MC-solvent. Matches the household's EXPLICIT,
        already-stated constraint — ``goals_yaml.retirement_drawdown_style
        = capital_preservation_returns_only`` / ``retirement_drawdown_note:
        "User explicitly stated no principal drawdown after retirement;
        portfolio must generate returns >= annual spend; equivalent to 0% SWR
        on principal"`` (verified live against ``UserContext.goals_yaml``,
        user_id=ariel).
      * ``retirement.earliest_safe_age`` (alias: ``retirement.drawdown_scenario_age``,
        kept as a literal duplicate key for surfaces already wired to it) —
        the OFF-MANDATE reading: typical Monte Carlo, 90% solvency to 95,
        PERMITS spending principal.

    An earlier draft of this fix unilaterally repointed
    ``retirement.earliest_safe_age`` to the preservation value and called it
    "the headline" — a structurally different PATH decision (46 vs 55) that
    Ariel correctly identified as needing HIS call, not an agent's. Ariel's
    ruling: neither age is "the" retirement age. Every surface that used to
    print one age must print the PAIR, each labeled by its discipline. A
    surface with room for only one number must show the mandate-satisfying
    ``preservation_age`` and say the off-mandate reading exists.

    ``retirement.fi_age`` (a SEPARATE key, populated in ``_apply_fi_methodology``)
    is an LLM-agent-authored field (``withdrawal_sequencer.fi_base.retirement_age``),
    not a deterministic reading of either policy, and often donor-inherited
    (stale). Per Ariel's ruling it must NEVER drive a published age or size a
    published FIRE bridge — see the two bridge keys below, both sized from a
    deterministic MC age, never from fi_age.

    Lazy import: the retirement engine and this resolver are mutually
    re-entrant, so the call is only reached from a top-level display surface
    (the caller gates it behind ``include_canonical_ages``). Best-effort — any
    failure (thin data, MC error, or no age clearing the bar) leaves the keys
    absent → pending sentinel. NEVER a fabricated or stale fallback value.
    """
    early_key = "retirement.earliest_safe_age"
    pres_key = "retirement.preservation_age"
    drawdown_key = "retirement.drawdown_scenario_age"
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

    drawdown_age = _to_float(getattr(canon, "earliest_feasible_age", None))
    p_solvent = getattr(canon, "p_solvent_at_age", None)
    if drawdown_age is not None:
        target_p = getattr(canon, "target_p_solvent", 0.90) or 0.90
        drawdown_formula = (
            "OFF-MANDATE reading — earliest age the typical-regime drawdown "
            f"Monte Carlo clears {target_p:.0%} solvency to 95 on the "
            "deconcentrated, reserve-netted, CGT-haircut basis; PERMITS "
            "spending principal — NOT the household's stated mandate. Always "
            "state alongside retirement.preservation_age (the mandate-"
            "satisfying reading); never state this age alone as 'the' "
            "retirement age."
        )
        for key in (early_key, drawdown_key):
            values[key] = ResolvedValue(
                key=key,
                value=drawdown_age,
                unit="age",
                status="resolved",
                source_locator="retirement_plan.canonical_feasible_dual_track.earliest_feasible_age",
                confidence="HIGH" if p_solvent is not None else "MEDIUM",
                formula=drawdown_formula,
            )

    basis = getattr(canon, "basis", None)
    pres_age = _to_float(basis.get("preservation_age")) if isinstance(basis, dict) else None
    if pres_age is not None:
        pres_p = _to_float(basis.get("preservation_p")) if isinstance(basis, dict) else None
        values[pres_key] = ResolvedValue(
            key=pres_key,
            value=pres_age,
            unit="age",
            status="resolved",
            source_locator="retirement_plan.canonical_feasible_dual_track.basis.preservation_age",
            confidence="HIGH" if pres_p is not None else "MEDIUM",
            formula=(
                "MANDATE-SATISFYING reading — earliest age the worst-10% real "
                "path preserves today's real principal to 95 (>=99% MC-solvent); "
                "operationalizes the household's explicit no-principal-drawdown "
                "mandate (goals_yaml.retirement_drawdown_style="
                "capital_preservation_returns_only) on the deconcentrated, "
                "reserve-netted, CGT-haircut basis. Always state alongside "
                "retirement.earliest_safe_age (the off-mandate reading); if a "
                "surface can show only one age, show THIS one."
            ),
        )

    # RED-12, per Ariel's ruling: publish TWO bridge figures, each sized from
    # the age it is presented against, each saying which. Neither is sized
    # from fi_age (Decision 2 — fi_age never sizes a published bridge).
    spend_rv = values.get("spend.fi_basis_nis")
    spend_ok = spend_rv is not None and spend_rv.status == "resolved" and spend_rv.value is not None
    if spend_ok:
        from argosy.services.cashflow_projection import LUMP_PENSION_AGE

        pres_rv = values.get(pres_key)
        if pres_rv is not None and pres_rv.status == "resolved" and pres_rv.value is not None:
            pres_age_v = float(pres_rv.value)
            bridge_years = max(0.0, float(LUMP_PENSION_AGE) - pres_age_v)
            values["retirement.fire_bridge_nis"] = ResolvedValue(
                key="retirement.fire_bridge_nis",
                value=bridge_years * float(spend_rv.value),
                unit="nis",
                status="resolved",
                source_locator=(
                    f"({LUMP_PENSION_AGE} - retirement.preservation_age[mandate]) "
                    "yrs x spend.fi_basis_nis"
                ),
                agent_report_id=None,
                confidence=pres_rv.confidence,
                formula=(
                    "FIRE bridge — MANDATE case: liquid drawdown to fund "
                    "permanent-equivalent spend from the capital-preservation "
                    "retirement age (retirement.preservation_age) to the age-60 "
                    "pension unlock. Never sized from fi_age."
                ),
            )

        early_rv = values.get(early_key)
        if early_rv is not None and early_rv.status == "resolved" and early_rv.value is not None:
            early_age_v = float(early_rv.value)
            bridge_years = max(0.0, float(LUMP_PENSION_AGE) - early_age_v)
            values["retirement.fire_bridge_offmandate_nis"] = ResolvedValue(
                key="retirement.fire_bridge_offmandate_nis",
                value=bridge_years * float(spend_rv.value),
                unit="nis",
                status="resolved",
                source_locator=(
                    f"({LUMP_PENSION_AGE} - retirement.earliest_safe_age[off-mandate]) "
                    "yrs x spend.fi_basis_nis"
                ),
                agent_report_id=None,
                confidence=early_rv.confidence,
                formula=(
                    "FIRE bridge — OFF-MANDATE case: liquid drawdown to fund "
                    "permanent-equivalent spend from the typical-drawdown "
                    "retirement age (retirement.earliest_safe_age) to the "
                    "age-60 pension unlock. Never sized from fi_age. Shown for "
                    "comparison only — the mandate-case bridge "
                    "(retirement.fire_bridge_nis) is the one that matches the "
                    "household's stated constraint."
                ),
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
    except Exception as exc:  # noqa: BLE001 — walkback gap → carry forward last-known
        # A missing DAILY rate (a feed-job gap > the 10-day walkback, a long
        # holiday, a cold cache) must NOT pending-out every NIS figure in the plan.
        # Carry forward the most-recent cached BOI rate (any age) — a stale-but-real
        # representative rate is vastly better than pending the whole plan — flagged
        # MEDIUM with an explicit "as of" provenance. Pending ONLY if the cache holds
        # no USD rate at all (never the magic 3.45).
        from argosy.state.models import FxRate

        last = session.execute(
            select(FxRate.date, FxRate.rate)
            .where(FxRate.currency == "USD")
            .order_by(FxRate.date.desc())
            .limit(1)
        ).first()
        if last is not None and last[1] is not None:
            as_of, rate = last[0], float(last[1])
            log.warning(
                "plan_numeric_resolver.fx_boi_carry_forward as_of=%s rate=%.4f (%s)",
                as_of, rate, exc,
            )
            values[key] = ResolvedValue(
                key=key, value=rate, unit="nis_per_usd", status="resolved",
                source_locator=f"{loc} — carried forward, as of {as_of} (no rate within walkback)",
                agent_report_id=None, confidence="MEDIUM",
                formula=f"Bank of Israel representative USD/NIS, last-known as of {as_of}",
            )
        else:
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
        from argosy.services.holding_books import (
            load_total_book,
            parse_positions_json,
        )
        from argosy.services.retirement.safety_gates import _us_situs_assets_usd

        snap = _head_snapshot_row(session, user_id)
        if snap is None:
            values[key] = ResolvedValue.pending(key, "nis", loc)
            return
        if _spine_gate_refuses(session, user_id, snap):
            values[key] = ResolvedValue.unavailable(
                key, "nis",
                "spine gate: head snapshot not validated (no PASS verdict)",
            )
            return
        raw_positions = parse_positions_json(snap.positions_json)
        book = load_total_book(
            session, user_id, raw_positions,
            snapshot_date=getattr(snap, "snapshot_date", None),
            # today defaults to the REAL current date — NEVER the snapshot's own
            # date. Backdating today=snapshot_date set every mark's age to 0, so
            # an 8-day-old book read as "fresh", no reprice fired, stale_marks
            # stayed empty, and estate/concentration republished stale money as
            # HIGH (Sol BLOCK-1). Real today lets soft-stale downgrade / hard
            # degrade honestly; a daily-refreshed book is genuinely fresh.
        )
        if book.degraded:
            # NEVER publish a HIGH-confidence understated estate figure.
            values[key] = ResolvedValue.unavailable(
                key, "nis",
                f"{loc} — DEGRADED: {book.degrade_reason}",
                formula=(
                    "unavailable: durable unmanaged book missing/unloadable "
                    "while snapshot omits a policy holding — refusing to "
                    "publish understated US-situs at HIGH confidence"
                ),
            )
            return
        # TOTAL book — US estate tax does not care which book manages an asset.
        positions = book.total
        usd = _us_situs_assets_usd(positions)
        # Mark to the SAME current-BOI-FX basis net worth uses (snapshot fx is
        # the fallback only when BOI is uncached) — one FX convention per book.
        snap_fx = _to_float(snap.fx_usd_nis) or 0.0
        fx, fx_src = _current_boi_usd_nis(session, snap_fx)
        if not usd or not fx or usd <= 0 or fx <= 0:
            values[key] = ResolvedValue.pending(key, "nis", loc)
            return
        # A soft-stale mark anywhere in the total book (NVDA dominates US-situs)
        # means this estate figure isn't fully current money — downgrade from
        # HIGH instead of republishing stale as HIGH (Sol BLOCK-1, estate half).
        _stale = book.stale_marks
        _conf = "MEDIUM" if _stale else "HIGH"
        _stale_note = (
            f" [STALE MARK — total book carries soft-stale marks "
            f"({', '.join(_stale)}); live reprice unavailable, confidence "
            f"downgraded from HIGH]"
            if _stale
            else ""
        )
        values[key] = ResolvedValue(
            key=key,
            value=usd * fx,
            unit="nis",
            status="resolved",
            source_locator=(
                f"{loc} = {fx_src} {fx:.3f} (snapshot id={snap.id}; total book)"
                + _stale_note
            ),
            agent_report_id=None,
            confidence=_conf,
            formula=(
                "Σ US-domiciled securities across ALL brokers (TOTAL book incl. "
                "deliberately unmanaged NVDA; by instrument domicile: NVDA + US "
                "ETFs + US single names at Schwab and the Israeli broker; UCITS / "
                "Israeli / cash excluded) per IRS NRA estate-tax rules, × current "
                "BOI USD/NIS (snapshot fx fallback)"
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
    convention) — OR an explicit unavailable status.

    NVDA is deliberately UNMANAGED (held at Schwab, out of the tradeable
    sleeve), but present-but-unmanaged is a CONCENTRATION fact, not an
    absence: NVDA COUNTS toward the single-name concentration weight (and
    toward US-situs estate). The weight is measured against the TOTAL book's
    tradeable-securities denominator (which INCLUDES the unmanaged NVDA
    position), so it resolves to the true ~58% — never ``excluded``/0.0,
    which would make the downstream deconcentration / cap / quota math run
    against zero. NVDA remaining OUT of the managed / tradeable / sell-eligible
    sleeve (never a BUY/SELL) is enforced separately by ``book.managed`` /
    ``is_managed_position``.

    When NVDA is missing from the total book entirely, status is
    ``unavailable`` — never a silent 0.0. Absolute NVDA value for FI shock /
    estate is published separately as ``concentration.nvda_value_nis``.
    """
    key = "concentration.nvda_current_pct"
    value_key = "concentration.nvda_value_nis"
    loc = "holding_books total book + wealth_dashboard.nvda_concentration_pct"
    try:
        from argosy.services.holding_books import (
            TotalBookDegraded,
            has_symbol,
            is_managed_position,
            load_total_book,
            parse_positions_json,
            symbol_value_usd_k,
        )
        from argosy.services.portfolio_snapshot_store import (
            get_latest_snapshot_row,
        )
        from argosy.services.wealth_dashboard import nvda_concentration_pct

        # Head-snapshot selection MUST match every other current-money surface
        # (portfolio route, dashboard, the repair script). A bare id.desc()
        # here diverged from the canonical (imported_at DESC, id DESC), so a
        # backfilled row with a higher id but older import could surface a
        # different NVDA % than /portfolio and bypass the repaired head
        # (Sol BLOCK-6). Route through the one canonical accessor.
        snap = get_latest_snapshot_row(session, user_id)
        if snap is None:
            return  # leave whatever the role resolver set (likely pending)
        if _spine_gate_refuses(session, user_id, snap):
            values[value_key] = ResolvedValue.unavailable(
                value_key, "nis",
                "spine gate: head snapshot not validated (no PASS verdict)",
            )
            values[key] = ResolvedValue.unavailable(
                key, "pct",
                "spine gate: head snapshot not validated (no PASS verdict)",
            )
            return
        raw_positions = parse_positions_json(snap.positions_json)
        book = load_total_book(
            session, user_id, raw_positions,
            snapshot_date=getattr(snap, "snapshot_date", None),
            # today defaults to the REAL current date — NEVER the snapshot's own
            # date. Backdating today=snapshot_date set every mark's age to 0, so
            # an 8-day-old book read as "fresh", no reprice fired, stale_marks
            # stayed empty, and estate/concentration republished stale money as
            # HIGH (Sol BLOCK-1). Real today lets soft-stale downgrade / hard
            # degrade honestly; a daily-refreshed book is genuinely fresh.
        )
        if book.degraded:
            values[value_key] = ResolvedValue.unavailable(
                value_key, "nis",
                f"total book degraded — refusing NVDA value: {book.degrade_reason}",
            )
            values[key] = ResolvedValue.unavailable(
                key, "pct",
                f"total book degraded — refusing NVDA weight: {book.degrade_reason}",
            )
            return
        total = book.total
        # A SOFT-stale NVDA mark (last-close published without a live reprice —
        # weekend/holiday/transient quote miss) does NOT degrade the book, but
        # it must NOT republish as HIGH-confidence CURRENT money (Sol BLOCK-1).
        # Downgrade confidence + annotate; the value still publishes (graceful).
        # NVDA VALUE (absolute) downgrades only when NVDA's OWN mark is stale.
        nvda_val_stale = book.is_mark_stale("NVDA")
        _val_conf = "MEDIUM" if nvda_val_stale else "HIGH"
        _val_note = (
            " [STALE MARK — NVDA published on a soft-stale last-known price; "
            "live reprice unavailable, confidence downgraded from HIGH]"
            if nvda_val_stale
            else ""
        )
        # NVDA WEIGHT = NVDA ÷ tradeable book: ANY soft-stale mark makes the
        # DENOMINATOR stale, so the weight downgrades on the whole book, not
        # just NVDA (Sol round-5 #6).
        _wt_conf = "MEDIUM" if book.stale_marks else "HIGH"
        _wt_note = (
            f" [STALE MARK — tradeable-book denominator includes soft-stale "
            f"marks ({', '.join(book.stale_marks)}); confidence downgraded "
            f"from HIGH]"
            if book.stale_marks
            else ""
        )
        snap_fx = _to_float(snap.fx_usd_nis) or 0.0
        fx, fx_src = _current_boi_usd_nis(session, snap_fx)

        nvda_k = symbol_value_usd_k(total, "NVDA")
        if nvda_k > 0 and fx and fx > 0:
            values[value_key] = ResolvedValue(
                key=value_key,
                value=nvda_k * 1000.0 * fx,
                unit="nis",
                status="resolved",
                source_locator=(
                    f"Σ NVDA usd_value_k on TOTAL book × {fx_src} {fx:.3f} "
                    f"(snapshot id={snap.id}){_val_note}"
                ),
                agent_report_id=None,
                confidence=_val_conf,
                formula="NVDA usd_value_k × 1000 × current BOI USD/NIS (total book)",
            )
        elif has_symbol(total, "NVDA"):
            values[value_key] = ResolvedValue.pending(
                value_key, "nis", "NVDA present but FX/value unresolved",
            )
        # else: leave value_key unset / prior — cold snapshot with no positions

        if not has_symbol(total, "NVDA"):
            # Distinguish cold/empty snapshot (leave agent-derived prior) from
            # a real book that silently omitted NVDA (must not publish 0.0).
            from argosy.services.wealth_dashboard import tradeable_securities_usd_k

            if tradeable_securities_usd_k(total) > 0:
                values[key] = ResolvedValue.unavailable(
                    key, "pct",
                    "NVDA absent from total book that has other securities — "
                    "not 0.0 (missing ≠ zero)",
                    formula="unavailable (missing ≠ zero)",
                )
            return

        # NVDA COUNTS toward concentration even when it is deliberately
        # UNMANAGED (held at Schwab, out of the tradeable sleeve). Its share of
        # the TOTAL investable book is exactly what drives the deconcentration /
        # cap / quota math, so reporting it as ``excluded`` (value=None) made
        # that math run against 0 and hid a ~58% single-name concentration. The
        # weight is computed against the TOTAL book's tradeable-securities
        # denominator (which INCLUDES the unmanaged NVDA position) — the same
        # ~58% the estate / net-worth surfaces already see. Present-but-unmanaged
        # is a CONCENTRATION fact, not an absence. NVDA staying OUT of the
        # managed/tradeable/sell-eligible sleeve (never a BUY/SELL) is enforced
        # separately by ``book.managed`` / ``is_managed_position`` — it must NOT
        # be expressed by zeroing this weight.
        nvda_rows = [
            p for p in total
            if str(p.get("symbol") or "").upper() == "NVDA"
        ]
        unmanaged = bool(nvda_rows) and not is_managed_position(nvda_rows[0])

        pct = nvda_concentration_pct(total)
        if pct is None:
            return
        formula = (
            "NVDA usd_value_k ÷ tradeable securities book (excl. cash + "
            "physical real estate), snapshot-derived — % of tradeable book"
        )
        if unmanaged:
            formula += (
                "; NVDA present-but-UNMANAGED — counted for concentration + "
                "US-situs estate on the TOTAL book, held OUT of the managed / "
                "tradeable / sell-eligible sleeve (never emitted as a BUY/SELL)"
            )
        loc_note = (
            f"{loc} — present-but-UNMANAGED (counted for concentration; "
            f"excluded from the managed sleeve) (snapshot id={snap.id})"
            if unmanaged
            else f"{loc} (snapshot id={snap.id})"
        )
        values[key] = ResolvedValue(
            key=key,
            value=pct / 100.0,  # stored as a fraction (0–1), unit "pct"
            unit="pct",
            status="resolved",
            source_locator=loc_note + _wt_note,
            agent_report_id=None,
            # Keep master's staleness-aware confidence (MEDIUM when marks are
            # stale) rather than the branch's hardcoded HIGH: an unmanaged
            # NVDA weight is only as trustworthy as the marks behind it.
            confidence=_wt_conf,
            formula=formula,
        )
    except TotalBookDegraded as exc:
        values[value_key] = ResolvedValue.unavailable(
            value_key, "nis", f"total book degraded: {exc.reason}",
        )
        values[key] = ResolvedValue.unavailable(
            key, "pct", f"total book degraded: {exc.reason}",
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
    # HONEST basis: liquid net worth (excludes ALL real estate) − FI total capital.
    # Using the real-estate-inclusive net_worth overstated sufficiency (+₪118K) and
    # claimed FI reached when the liquid basis is actually short (−₪148K) — the codex
    # BLOCK on draft 45/46. FI sufficiency must be tested on spendable capital only.
    loc = "portfolio.liquid_net_worth_nis − retirement.fi_total_capital_nis"
    nw = values.get("portfolio.liquid_net_worth_nis")
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
    # Codex AMBER (run 379): "the margin silently changed" — a margin that moves
    # between runs without stating why. Fix: state the two live input VALUES (not
    # just their key names) and what makes each one move, so a run-to-run delta is
    # traceable to a cause instead of reading as drift. This margin has exactly two
    # inputs, both already resolved+sourced elsewhere: it moves ONLY when (a) the
    # portfolio mark-to-market changes (new snapshot import, NVDA/other price moves,
    # FX) — that's portfolio.liquid_net_worth_nis, see its own source_locator — or
    # (b) the FI methodology's inputs change (identity_yaml tracked spend edited,
    # goals_yaml education/liability figures edited, or the SWR/life-event planning
    # constants in fi_methodology.py are edited) — that's retirement.fi_total_capital_nis.
    # It is NEVER an LLM-authored number and never moves for any other reason.
    formula = (
        "liquid_net_worth_nis − fi_total_capital_nis (signed; >0 => total target reached; "
        "LIQUID basis, excl. real estate) | "
        f"as-of this run: liquid_net_worth_nis=₪{float(nw.value):,.0f} "
        f"(moves with portfolio mark-to-market: new snapshot import / price / FX — "
        f"see portfolio.liquid_net_worth_nis.source_locator), "
        f"fi_total_capital_nis=₪{float(tot.value):,.0f} "
        f"(moves only if the FI methodology inputs change — tracked spend, education/"
        f"liability figures, or the SWR/life-event constants in fi_methodology.py — "
        f"see retirement.fi_total_capital_nis.source_locator); "
        "a margin delta vs. a prior run is caused by one or both of these moving, "
        "never by an independent re-derivation"
    )
    values[key] = ResolvedValue(
        key=key,
        value=float(nw.value) - float(tot.value),
        unit="nis",
        status="resolved",
        source_locator=loc,
        agent_report_id=None,
        confidence="HIGH",
        formula=formula,
    )


def _apply_nvda_realization_tax(
    session: "Session", user_id: str, values: dict[str, ResolvedValue]
) -> None:
    """Derive the embedded Israeli tax liability on Ariel's entire NVDA position.

    Uses the latest ingested tax-simulation report (tax_simulation_lots table).  Publishes:

    - ``tax.nvda_embedded_cgt_nis`` — total Israeli tax the user would owe if realizing
      ALL NVDA shares TODAY, in NIS at the current BOI USD/NIS rate.
    - ``tax.nvda_net_proceeds_nis`` — what actually arrives after all Israeli taxes
      (§102 Capital track 30% effective on eligible lots; implied effective rate on
      breaking lots from the simulation itself).

    Revaluation at current price (not stale at $204.65):
    Eligible lots: ordinary income is fixed at grant-date FMV.  Only the capital slice
    shifts; ΔEmbedded_tax = 30% × (current_price − sim_price) × shares.
    Breaking lots: per-lot implied effective rate from the simulation is applied to the
    new gross — avoids re-deriving the NI ceiling stack the trustee already computed.

    If the current NVDA price is unavailable from the snapshot, falls back to the
    simulation price; the source_locator documents which price was used.

    Both keys are ``status="pending"`` (never fabricated) when the simulation table is
    empty or FX is unavailable.
    """
    key_tax = "tax.nvda_embedded_cgt_nis"
    key_net = "tax.nvda_net_proceeds_nis"
    pending_loc = "tax_simulation_lots + BOI FX + NVDA snapshot price"

    # FX — MUST be the same rate the gross margin used, or gross and net are
    # not comparable and their difference is partly an FX artefact. Sol found
    # (and a live check confirmed) that liquid net worth converts via
    # _current_boi_usd_nis(snapshot_fx) while this path was using the resolved
    # fx.usd_nis key, whose fallback differs — 2.998 vs 2.9566 on the live book,
    # a 1.4% gap silently embedded in a headline the user reads as a subtraction.
    fx = 0.0
    try:
        _snap_for_fx = _head_snapshot_row(session, user_id)
        if _snap_for_fx is not None:
            _snap_fx = _to_float(_snap_for_fx.fx_usd_nis) or 0.0
            fx, _ = _current_boi_usd_nis(session, _snap_fx)
    except Exception:  # noqa: BLE001 — fall through to the resolved key below
        fx = 0.0
    if not fx or fx <= 0:
        fx_rv = values.get("fx.usd_nis")
        if fx_rv is None or fx_rv.status != "resolved" or not fx_rv.value:
            for k in (key_tax, key_net):
                values[k] = ResolvedValue.pending(k, "nis", "fx.usd_nis not resolved")
            return
        fx = float(fx_rv.value)

    # Try to read the current NVDA price AND share count from the snapshot book.
    # Both are needed: price for revaluation; shares to detect sim/held divergence (blocker 4).
    current_nvda_px: float | None = None
    actual_nvda_shares: float | None = None
    _book_stale = False  # blocker 5: stale marks downgrade confidence
    try:
        from argosy.services.holding_books import (
            TotalBookDegraded,
            load_total_book,
            parse_positions_json,
        )

        snap = _head_snapshot_row(session, user_id)
        if snap is not None:
            raw = parse_positions_json(snap.positions_json)
            book = load_total_book(
                session, user_id, raw,
                snapshot_date=getattr(snap, "snapshot_date", None),
            )
            if not book.degraded:
                # Blocker 5: soft-stale marks mean the NVDA price may be stale.
                # A stale price understates the current capital gain and therefore
                # the embedded tax — always the dangerous direction for Ariel.
                _book_stale = bool(book.stale_marks)
                for p in book.total:
                    if str(p.get("symbol", "")).upper() == "NVDA":
                        px = _to_float(p.get("current_price"))
                        if px and px > 0:
                            current_nvda_px = px
                        # Blocker 4: capture actual held shares for scope check
                        sh = _to_float(p.get("shares"))
                        if sh and sh > 0:
                            actual_nvda_shares = sh
                        break
    except Exception as exc:  # noqa: BLE001 — degrade gracefully
        log.warning("plan_numeric_resolver.realization_tax_nvda_price_failed err=%s", exc)

    # Aggregate from the tax-simulation table.
    try:
        from argosy.services.tax_simulation_ingest import realization_tax_summary

        agg = realization_tax_summary(
            session, user_id, current_nvda_price_usd=current_nvda_px
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("plan_numeric_resolver.realization_tax_summary_failed err=%s", exc)
        agg = None

    if agg is None:
        for k in (key_tax, key_net):
            values[k] = ResolvedValue.pending(k, "nis", "no tax-sim report ingested")
        return

    # Blocker 4: compare simulation share count with actual held shares.
    # If they diverge (tranche sold, new vest landed), the figure is scoped to
    # the simulation shares only — confidence degrades and the scope is stated.
    # _shares_match starts as None (unknown) — only True when actual shares are
    # found AND match within 1 share. Unknown counts as not-confirmed → MEDIUM.
    _share_scope_note = ""
    _shares_match: bool | None = None
    if actual_nvda_shares is not None:
        divergence = abs(actual_nvda_shares - agg.total_shares)
        if divergence > 1.0:
            _share_scope_note = (
                f" [SCOPE: sim covers {agg.total_shares:,.0f} sh; "
                f"snapshot shows {actual_nvda_shares:,.0f} sh held — "
                f"{divergence:,.0f}-sh divergence; tax figure scoped to sim shares only]"
            )
            _shares_match = False
        else:
            _shares_match = True
    else:
        # Could not read actual NVDA share count from the book (book unavailable
        # or NVDA not present in the snapshot) — we cannot confirm the sim covers
        # all held shares. Treat as unknown (not confirmed), not as a match.
        _share_scope_note = (
            " [SCOPE: actual NVDA share count not found in book — "
            "cannot confirm sim shares match held shares; confidence degraded]"
        )

    # Convert to NIS using the resolver's canonical FX rate.
    embedded_tax_nis = agg.embedded_tax_at_revalue_usd * fx
    net_proceeds_nis = agg.net_at_revalue_usd * fx

    price_label = (
        f"REVALUED at current NVDA ${agg.revalue_price_usd:,.2f}"
        if agg.uses_current_price
        else f"AS-OF sim date {agg.simulation_date} @ ${agg.sim_sale_price_usd:,.2f} "
             f"(current NVDA price unavailable — tax UNDERSTATED at higher current price)"
    )
    stale_note = (
        f" [STALE MARK — book carries soft-stale marks; current NVDA price may be "
        f"stale, understating the capital gain → tax understated]"
        if _book_stale else ""
    )
    # Incomplete lots: some shares could not be fully accounted for in the tax
    # computation (missing net_proceeds_usd OR breaking lots without ordinary_income
    # for revaluation). Non-zero means the embedded tax figure covers fewer shares
    # than total_shares, so it is understated — always the dangerous direction.
    _incomplete_note = (
        f" [INCOMPLETE: {agg.incomplete_lot_shares:,.0f} sh excluded from tax "
        f"computation (missing net_proceeds or ordinary_income for revaluation) — "
        f"embedded tax understated]"
        if agg.incomplete_lot_shares > 0 else ""
    )
    source_loc = (
        f"tax_simulation_lots {agg.simulation_date} @ ${agg.sim_sale_price_usd:,.2f} | "
        f"{price_label} | "
        f"{agg.total_shares:,.0f} sh × BOI FX {fx:.4f} NIS/USD"
        f"{_share_scope_note}{stale_note}{_incomplete_note}"
    )

    # Confidence: HIGH only when ALL four conditions hold:
    #   - price was revalued to current mark (not left at stale sim date)
    #   - book carries no soft-stale marks (blocker 5)
    #   - simulation share count confirmed to match held shares (blocker 4)
    #     — _shares_match=True requires actual count found AND within 1 share;
    #       _shares_match=None (unknown, book unavailable) → MEDIUM, not HIGH
    #   - no incomplete lots (all shares fully accounted for in tax computation)
    # Every condition that fails errs toward a SMALLER embedded tax (flatters Ariel).
    confidence = (
        "HIGH"
        if (
            agg.uses_current_price
            and not _book_stale
            and _shares_match is True
            and agg.incomplete_lot_shares == 0
        )
        else "MEDIUM"
    )

    values[key_tax] = ResolvedValue(
        key=key_tax,
        value=embedded_tax_nis,
        unit="nis",
        status="resolved",
        source_locator=source_loc,
        agent_report_id=None,
        confidence=confidence,
        formula=(
            f"sum(gross − net_proceeds per lot) at {price_label} × BOI USD/NIS; "
            "eligible lots revalued at §102 Capital 30% effective rate on Δcapital; "
            "breaking lots at per-lot implied effective rate from trustee sim"
        ),
    )
    values[key_net] = ResolvedValue(
        key=key_net,
        value=net_proceeds_nis,
        unit="nis",
        status="resolved",
        source_locator=source_loc,
        agent_report_id=None,
        confidence=confidence,
        formula=(
            f"sum(net_proceeds_usd per lot) at {price_label} × BOI USD/NIS; "
            "what survives after all Israeli §102 + surtax on the full NVDA position"
        ),
    )


def _apply_nvda_realization_tax_glide(
    session: "Session", user_id: str, values: dict[str, ResolvedValue]
) -> None:
    """Glide-consistent counterpart to ``_apply_nvda_realization_tax``.

    The full-liquidation figure (``tax.nvda_embedded_cgt_nis``) taxes ALL 10,940 NVDA
    shares — a useful UPPER BOUND, but not what the plan actually does: the
    deconcentration glide sells ``concentration.nvda_sell_sh`` shares (retaining
    ``concentration.nvda_target_sh`` at the 8%-class target), and only
    ``concentration.nvda_eligible_now_sh`` of those are Section-102 capital-track
    TODAY — the rest of the planned sale is Breaking (ordinary-income) treatment.
    The FM's rejection (run 379) is that subtracting the full-liquidation tax from
    the gross margin overstates the drag and can wrongly read as "FI not close."

    Publishes ``tax.nvda_embedded_cgt_glide_nis`` = tax on ONLY the planned sale
    (``nvda_sell_sh`` shares), computed by capping ``realization_tax_summary`` at
    ``nvda_eligible_now_sh`` capital-track shares + the remaining
    (``nvda_sell_sh`` − ``nvda_eligible_now_sh``) shares at Breaking/ordinary
    treatment — the two rates are NEVER blended (per-group caps, summed).

    Tax-year spacing: the glide sells shares "spaced across tax years" per the plan,
    which would let some tranches clear the surtax threshold in a later year at a
    lower marginal rate. That per-year schedule is NOT sourced anywhere in this
    codebase (no dated sale-lot plan exists), so this function does NOT invent one —
    it computes the SINGLE-YEAR case (as if the whole planned sale realizes in one tax
    year, the same convention the full-liquidation figure already uses) and says so
    explicitly in the source_locator. That makes this a conservative (upper) bound on
    the glide-consistent tax, not a promise that multi-year spacing wouldn't do better.
    """
    key = "tax.nvda_embedded_cgt_glide_nis"

    sell_rv = values.get("concentration.nvda_sell_sh")
    elig_rv = values.get("concentration.nvda_eligible_now_sh")
    if (
        sell_rv is None or elig_rv is None
        or sell_rv.status != "resolved" or elig_rv.status != "resolved"
        or sell_rv.value is None or elig_rv.value is None
    ):
        values[key] = ResolvedValue.pending(
            key, "nis", "concentration.nvda_sell_sh or nvda_eligible_now_sh not resolved",
        )
        return

    sell_sh = float(sell_rv.value)
    eligible_cap = min(float(elig_rv.value), sell_sh)
    breaking_cap = max(0.0, sell_sh - eligible_cap)

    # Same FX + current-price sourcing as the full-liquidation path, so the two
    # figures are comparable (only the share scope differs).
    fx = 0.0
    try:
        _snap_for_fx = _head_snapshot_row(session, user_id)
        if _snap_for_fx is not None:
            _snap_fx = _to_float(_snap_for_fx.fx_usd_nis) or 0.0
            fx, _ = _current_boi_usd_nis(session, _snap_fx)
    except Exception:  # noqa: BLE001
        fx = 0.0
    if not fx or fx <= 0:
        fx_rv = values.get("fx.usd_nis")
        if fx_rv is None or fx_rv.status != "resolved" or not fx_rv.value:
            values[key] = ResolvedValue.pending(key, "nis", "fx.usd_nis not resolved")
            return
        fx = float(fx_rv.value)

    current_nvda_px: float | None = None
    try:
        from argosy.services.holding_books import load_total_book, parse_positions_json

        snap = _head_snapshot_row(session, user_id)
        if snap is not None:
            raw = parse_positions_json(snap.positions_json)
            book = load_total_book(
                session, user_id, raw,
                snapshot_date=getattr(snap, "snapshot_date", None),
            )
            if not book.degraded:
                for p in book.total:
                    if str(p.get("symbol", "")).upper() == "NVDA":
                        px = _to_float(p.get("current_price"))
                        if px and px > 0:
                            current_nvda_px = px
                        break
    except Exception as exc:  # noqa: BLE001
        log.warning("plan_numeric_resolver.realization_tax_glide_price_failed err=%s", exc)

    try:
        from argosy.services.tax_simulation_ingest import realization_tax_summary

        agg = realization_tax_summary(
            session, user_id, current_nvda_price_usd=current_nvda_px,
            max_eligible_shares=eligible_cap, max_breaking_shares=breaking_cap,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("plan_numeric_resolver.realization_tax_glide_summary_failed err=%s", exc)
        agg = None

    if agg is None:
        values[key] = ResolvedValue.pending(key, "nis", "no tax-sim report ingested")
        return

    embedded_tax_nis = agg.embedded_tax_at_revalue_usd * fx
    price_label = (
        f"REVALUED at current NVDA ${agg.revalue_price_usd:,.2f}"
        if agg.uses_current_price
        else f"AS-OF sim date {agg.simulation_date} @ ${agg.sim_sale_price_usd:,.2f}"
    )
    source_loc = (
        f"PLANNED SALE ONLY (glide-consistent, NOT full liquidation): "
        f"{eligible_cap:,.0f} sh capital-track (Section 102, 30% effective) + "
        f"{breaking_cap:,.0f} sh Breaking/ordinary (~{agg.total_shares - eligible_cap:,.0f} sh "
        f"priced at the per-lot implied Breaking rate) = {agg.total_shares:,.0f} sh of "
        f"concentration.nvda_sell_sh={sell_sh:,.0f} | tax_simulation_lots {agg.simulation_date} "
        f"@ ${agg.sim_sale_price_usd:,.2f} | {price_label} | BOI FX {fx:.4f} NIS/USD | "
        f"SINGLE-YEAR ASSUMPTION: the plan spaces sales across tax years, but no dated "
        f"per-year sale schedule is sourced in this codebase — this figure taxes the "
        f"entire planned sale as if realized in ONE tax year (a conservative upper bound "
        f"on the glide-consistent tax; multi-year spacing that clears the surtax "
        f"threshold in a later year would tax less)."
    )
    values[key] = ResolvedValue(
        key=key,
        value=embedded_tax_nis,
        unit="nis",
        status="resolved",
        source_locator=source_loc,
        agent_report_id=None,
        confidence="MEDIUM",  # never HIGH: single-year assumption + eligibility-group cap are documented estimates
        formula=(
            f"realization_tax_summary(max_eligible_shares={eligible_cap:,.0f}, "
            f"max_breaking_shares={breaking_cap:,.0f}) at {price_label} × BOI USD/NIS "
            "— tax on the PLANNED sale only, eligible and breaking shares taxed at "
            "their own rates (never blended)"
        ),
    )


def _apply_nvda_realization_tax_glide_dated(
    session: "Session", user_id: str, values: dict[str, ResolvedValue]
) -> None:
    """DATED counterpart to ``_apply_nvda_realization_tax_glide``.

    RED-9 (Sol, plan-109 review): the ``concentration.nvda_eligible_now_sh`` pool
    (9,230 sh) is a POINT-IN-TIME snapshot from the ingested tax-sim report, but
    Section-102 eligibility is time-varying — each Breaking lot matures 24 months
    from ITS OWN grant date (Amendment 147; see
    ``domain_knowledge/tax/israel/section_102.md``). The single-year glide tax
    above prices the ``nvda_sell_sh − nvda_eligible_now_sh`` shortfall at whatever
    Breaking lot happens to sort first (a documented but ARBITRARY lot-order
    assumption — see ``_cap_group_shares`` docstring), because it has no dated
    schedule to consult.

    This publishes the SAME glide-consistent tax, but computed against the DATED
    eligibility projected to the end of NEXT tax year (the horizon of the settled
    2-year glide verdict, ``action_proposals`` ``plan_glide_schedule_verdict``) —
    ``tax_simulation_ingest.dated_eligible_shares`` / ``realization_tax_summary(...,
    as_of_date=...)``. Shares with no parseable grant date (ESPP Breaking lots)
    never season under this projection — conservatively excluded, not assumed.

    Adds ``tax.nvda_embedded_cgt_glide_dated_nis`` and
    ``concentration.nvda_eligible_by_glide_horizon_sh`` ALONGSIDE the single-year
    figures above — never replacing them (same doctrine as the glide vs
    full-liquidation pair). Pending — never a guess — when inputs are missing.
    """
    from datetime import date as _date

    tax_key = "tax.nvda_embedded_cgt_glide_dated_nis"
    horizon_key = "concentration.nvda_eligible_by_glide_horizon_sh"

    sell_rv = values.get("concentration.nvda_sell_sh")
    if (
        sell_rv is None or sell_rv.status != "resolved" or sell_rv.value is None
    ):
        values[tax_key] = ResolvedValue.pending(
            tax_key, "nis", "concentration.nvda_sell_sh not resolved")
        values[horizon_key] = ResolvedValue.pending(
            horizon_key, "shares", "concentration.nvda_sell_sh not resolved")
        return

    sell_sh = float(sell_rv.value)
    as_of = _date(_date.today().year + 1, 12, 31)

    from argosy.services.tax_simulation_ingest import dated_eligible_shares

    try:
        dated_elig = dated_eligible_shares(session, user_id, as_of)
    except Exception as exc:  # noqa: BLE001
        log.warning("plan_numeric_resolver.dated_eligible_failed err=%s", exc)
        dated_elig = None

    if dated_elig is None:
        values[tax_key] = ResolvedValue.pending(
            tax_key, "nis", "no tax-sim report ingested")
        values[horizon_key] = ResolvedValue.pending(
            horizon_key, "shares", "no tax-sim report ingested")
        return

    values[horizon_key] = ResolvedValue(
        key=horizon_key, value=dated_elig, unit="shares", status="resolved",
        source_locator=(
            f"tax_simulation_ingest.dated_eligible_shares(as_of={as_of.isoformat()}) "
            "— currently-eligible pool + Breaking lots whose grant_date+24mo matures "
            "by that date (ESPP Breaking lots excluded: no parseable grant date)"
        ),
        confidence="HIGH",
        formula="eligible_shares(now) + sum(dated Breaking tranches maturing by the glide horizon)",
    )

    eligible_cap = min(dated_elig, sell_sh)
    breaking_cap = max(0.0, sell_sh - eligible_cap)

    fx = 0.0
    try:
        _snap_for_fx = _head_snapshot_row(session, user_id)
        if _snap_for_fx is not None:
            _snap_fx = _to_float(_snap_for_fx.fx_usd_nis) or 0.0
            fx, _ = _current_boi_usd_nis(session, _snap_fx)
    except Exception:  # noqa: BLE001
        fx = 0.0
    if not fx or fx <= 0:
        fx_rv = values.get("fx.usd_nis")
        if fx_rv is None or fx_rv.status != "resolved" or not fx_rv.value:
            values[tax_key] = ResolvedValue.pending(tax_key, "nis", "fx.usd_nis not resolved")
            return
        fx = float(fx_rv.value)

    current_nvda_px: float | None = None
    try:
        from argosy.services.holding_books import load_total_book, parse_positions_json

        snap = _head_snapshot_row(session, user_id)
        if snap is not None:
            raw = parse_positions_json(snap.positions_json)
            book = load_total_book(
                session, user_id, raw,
                snapshot_date=getattr(snap, "snapshot_date", None),
            )
            if not book.degraded:
                for p in book.total:
                    if str(p.get("symbol", "")).upper() == "NVDA":
                        px = _to_float(p.get("current_price"))
                        if px and px > 0:
                            current_nvda_px = px
                        break
    except Exception as exc:  # noqa: BLE001
        log.warning("plan_numeric_resolver.realization_tax_glide_dated_price_failed err=%s", exc)

    try:
        from argosy.services.tax_simulation_ingest import realization_tax_summary

        agg = realization_tax_summary(
            session, user_id, current_nvda_price_usd=current_nvda_px,
            max_eligible_shares=eligible_cap, max_breaking_shares=breaking_cap,
            as_of_date=as_of,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("plan_numeric_resolver.realization_tax_glide_dated_summary_failed err=%s", exc)
        agg = None

    if agg is None:
        values[tax_key] = ResolvedValue.pending(tax_key, "nis", "no tax-sim report ingested")
        return

    embedded_tax_nis = agg.embedded_tax_at_revalue_usd * fx
    price_label = (
        f"REVALUED at current NVDA ${agg.revalue_price_usd:,.2f}"
        if agg.uses_current_price
        else f"AS-OF sim date {agg.simulation_date} @ ${agg.sim_sale_price_usd:,.2f}"
    )
    values[tax_key] = ResolvedValue(
        key=tax_key,
        value=embedded_tax_nis,
        unit="nis",
        status="resolved",
        source_locator=(
            f"DATED glide (as of {as_of.isoformat()}, the settled 2-year glide "
            f"horizon): {eligible_cap:,.0f} sh capital-track (§102, 30% effective; "
            f"includes lots projected to mature by {as_of.isoformat()}) + "
            f"{breaking_cap:,.0f} sh still-Breaking/ordinary "
            f"(no parseable grant date, e.g. ESPP) = {agg.total_shares:,.0f} sh of "
            f"concentration.nvda_sell_sh={sell_sh:,.0f} | tax_simulation_lots "
            f"{agg.simulation_date} @ ${agg.sim_sale_price_usd:,.2f} | {price_label} | "
            f"BOI FX {fx:.4f} NIS/USD"
        ),
        agent_report_id=None,
        confidence="MEDIUM",  # projection of future maturity dates, not a live report marking
        formula=(
            f"realization_tax_summary(max_eligible_shares={eligible_cap:,.0f}, "
            f"max_breaking_shares={breaking_cap:,.0f}, as_of_date={as_of.isoformat()}) "
            f"at {price_label} × BOI USD/NIS — DATED counterpart of "
            "tax.nvda_embedded_cgt_glide_nis: eligibility is projected forward per-lot "
            "via the Section-102 24-months-from-grant clock instead of frozen at the "
            "report's own point-in-time markings"
        ),
    )


def _apply_fi_margin_net_of_realization_glide_dated(values: dict[str, ResolvedValue]) -> None:
    """DATED counterpart to ``_apply_fi_margin_net_of_realization_glide`` — same
    construction (gross margin − embedded tax) but netting
    ``tax.nvda_embedded_cgt_glide_dated_nis`` instead of the single-year figure.
    Alongside, never replacing, the single-year glide margin."""
    key = "retirement.fi_margin_net_of_realization_glide_dated_nis"
    gross_margin_rv = values.get("retirement.fi_margin_signed_nis")
    dated_tax_rv = values.get("tax.nvda_embedded_cgt_glide_dated_nis")

    if (
        gross_margin_rv is None
        or dated_tax_rv is None
        or gross_margin_rv.status != "resolved"
        or dated_tax_rv.status != "resolved"
        or gross_margin_rv.value is None
        or dated_tax_rv.value is None
    ):
        values[key] = ResolvedValue.pending(
            key, "nis",
            "retirement.fi_margin_signed_nis or tax.nvda_embedded_cgt_glide_dated_nis not resolved",
        )
        return

    net_margin = float(gross_margin_rv.value) - float(dated_tax_rv.value)
    tax_src = dated_tax_rv.source_locator or "tax_simulation_lots (dated glide)"
    values[key] = ResolvedValue(
        key=key,
        value=net_margin,
        unit="nis",
        status="resolved",
        source_locator=(
            f"retirement.fi_margin_signed_nis − tax.nvda_embedded_cgt_glide_dated_nis | {tax_src}"
        ),
        agent_report_id=None,
        confidence=dated_tax_rv.confidence or "MEDIUM",
        formula=(
            "fi_margin_signed_nis − nvda_embedded_cgt_glide_dated_nis "
            "(gross margin less the DATED glide-consistent tax — eligibility projected "
            "forward per-lot to the settled glide horizon rather than frozen at the "
            "report's point-in-time markings; "
            ">0 => FI reached under the plan as written; "
            "<0 => the plan as written does not yet clear FI after tax)"
        ),
    )


def _apply_fi_margin_net_of_realization(values: dict[str, ResolvedValue]) -> None:
    """FI sufficiency margin NET of the embedded NVDA realization tax.

    The gross margin (``retirement.fi_margin_signed_nis``) reports FI as reached when
    liquid net worth exceeds the FI total capital target — but liquid NW includes the
    ENTIRE NVDA position at market value, before the Israeli tax due on realization.
    To actually *spend* those proceeds the user must first pay the tax.

    Net-of-realization margin = fi_margin_signed_nis − tax.nvda_embedded_cgt_nis

    Equivalently: (liquid_NW − embedded_tax) − fi_total_capital.

    A positive value means FI is reached even after paying full realization taxes.
    A negative value means the FI claim is GROSS-ONLY and overstates true sufficiency.
    Both the gross and net margins are published; neither replaces the other (requirement
    R1: add, never replace).
    """
    key = "retirement.fi_margin_net_of_realization_nis"
    gross_margin_rv = values.get("retirement.fi_margin_signed_nis")
    embedded_tax_rv = values.get("tax.nvda_embedded_cgt_nis")

    if (
        gross_margin_rv is None
        or embedded_tax_rv is None
        or gross_margin_rv.status != "resolved"
        or embedded_tax_rv.status != "resolved"
        or gross_margin_rv.value is None
        or embedded_tax_rv.value is None
    ):
        values[key] = ResolvedValue.pending(
            key, "nis",
            "retirement.fi_margin_signed_nis or tax.nvda_embedded_cgt_nis not resolved",
        )
        return

    net_margin = float(gross_margin_rv.value) - float(embedded_tax_rv.value)
    tax_src = embedded_tax_rv.source_locator or "tax_simulation_lots"
    values[key] = ResolvedValue(
        key=key,
        value=net_margin,
        unit="nis",
        status="resolved",
        source_locator=(
            f"retirement.fi_margin_signed_nis − tax.nvda_embedded_cgt_nis | {tax_src}"
        ),
        agent_report_id=None,
        confidence=embedded_tax_rv.confidence or "HIGH",
        formula=(
            "fi_margin_signed_nis − nvda_embedded_cgt_nis "
            "(gross margin less the full Israeli tax liability on the NVDA position; "
            ">0 => FI reached even after realization tax; "
            "<0 => gross-only FI claim overstates true sufficiency)"
        ),
    )


def _apply_fi_margin_net_of_realization_glide(values: dict[str, ResolvedValue]) -> None:
    """Glide-consistent counterpart to ``_apply_fi_margin_net_of_realization``.

    Same construction (gross margin − embedded tax) but using
    ``tax.nvda_embedded_cgt_glide_nis`` (tax on the PLANNED sale only —
    ``concentration.nvda_sell_sh`` shares, eligible/breaking split respected) instead
    of ``tax.nvda_embedded_cgt_nis`` (full 10,940-share liquidation). This is the
    figure the FM asked for (run 379): "present BOTH a glide-consistent after-tax
    margin AND the full-liquidation bound" — this is the former; the latter is
    ``retirement.fi_margin_net_of_realization_nis``. Neither replaces the other.

    NOTE: this margin nets the tax on the PLANNED SALE, but the gross margin's net
    worth still includes the RETAINED shares (``concentration.nvda_target_sh``) at
    full mark, untaxed — those are held, not realized, so that is intentional and
    matches how the retained sleeve is treated everywhere else in the plan.
    """
    key = "retirement.fi_margin_net_of_realization_glide_nis"
    gross_margin_rv = values.get("retirement.fi_margin_signed_nis")
    glide_tax_rv = values.get("tax.nvda_embedded_cgt_glide_nis")

    if (
        gross_margin_rv is None
        or glide_tax_rv is None
        or gross_margin_rv.status != "resolved"
        or glide_tax_rv.status != "resolved"
        or gross_margin_rv.value is None
        or glide_tax_rv.value is None
    ):
        values[key] = ResolvedValue.pending(
            key, "nis",
            "retirement.fi_margin_signed_nis or tax.nvda_embedded_cgt_glide_nis not resolved",
        )
        return

    net_margin = float(gross_margin_rv.value) - float(glide_tax_rv.value)
    tax_src = glide_tax_rv.source_locator or "tax_simulation_lots (planned sale)"
    values[key] = ResolvedValue(
        key=key,
        value=net_margin,
        unit="nis",
        status="resolved",
        source_locator=(
            f"retirement.fi_margin_signed_nis − tax.nvda_embedded_cgt_glide_nis | {tax_src}"
        ),
        agent_report_id=None,
        confidence=glide_tax_rv.confidence or "MEDIUM",
        formula=(
            "fi_margin_signed_nis − nvda_embedded_cgt_glide_nis "
            "(gross margin less the tax on the PLANNED sale only — nvda_sell_sh shares, "
            "eligible/breaking split respected, retained shares untaxed; "
            "THE PLAN-AS-WRITTEN after-tax margin, as opposed to the full-liquidation bound; "
            ">0 => FI reached under the plan as written; "
            "<0 => the plan as written does not yet clear FI after tax)"
        ),
    )


def _apply_fi_shock_net_worths(values: dict[str, ResolvedValue]) -> None:
    """Publish the gate's primary shocked net-worth figures as resolvable keys.

    Same arithmetic as ``plan_output_gate`` / ``fi_shock`` — so the synthesizer
    can cite ``retirement.fi_shock_net_worth_nis`` / ``fi_fx_shock_net_worth_nis``
    instead of writing ``[derivation pending]``.
    """
    from argosy.services.retirement.fi_shock import (
        PRIMARY_FX_SHOCK,
        PRIMARY_NVDA_SHOCK,
        derive_fx_shock_inputs,
        derive_nvda_shock_inputs,
        primary_fx_shock_net_worth_nis,
        primary_nvda_shock_net_worth_nis,
    )

    class _DictResolved:
        def get(self, key: str):
            return values.get(key)

    resolved = _DictResolved()
    nvda_key = "retirement.fi_shock_net_worth_nis"
    fx_key = "retirement.fi_fx_shock_net_worth_nis"

    nvda_inputs = derive_nvda_shock_inputs(resolved)
    if nvda_inputs is None:
        values[nvda_key] = ResolvedValue.pending(
            nvda_key, "nis", "NVDA-shock inputs pending",
        )
    else:
        nw = primary_nvda_shock_net_worth_nis(
            net_worth_nis=nvda_inputs["net_worth_nis"],
            nvda_value_nis=nvda_inputs["nvda_value_nis"],
            shock=PRIMARY_NVDA_SHOCK,
        )
        values[nvda_key] = ResolvedValue(
            key=nvda_key,
            value=nw,
            unit="nis",
            status="resolved",
            source_locator=(
                f"portfolio.net_worth_nis − {PRIMARY_NVDA_SHOCK:.0%}×NVDA"
            ),
            agent_report_id=None,
            confidence="HIGH",
            formula=(
                f"net_worth − {PRIMARY_NVDA_SHOCK:.0%} × "
                "concentration.nvda_value_nis (TOTAL book; fallback "
                "net_worth × nvda_current_pct) — gate shock_0.30 row"
            ),
        )

    fx_inputs = derive_fx_shock_inputs(resolved)
    if fx_inputs is None:
        values[fx_key] = ResolvedValue.pending(
            fx_key, "nis", "FX-shock inputs pending",
        )
    else:
        nw = primary_fx_shock_net_worth_nis(
            net_worth_nis=fx_inputs["net_worth_nis"],
            usd_exposure_nis=fx_inputs["usd_exposure_nis"],
            fx_shock=PRIMARY_FX_SHOCK,
        )
        values[fx_key] = ResolvedValue(
            key=fx_key,
            value=nw,
            unit="nis",
            status="resolved",
            source_locator=(
                f"portfolio.net_worth_nis − {PRIMARY_FX_SHOCK:.0%}×USD exposure"
            ),
            agent_report_id=None,
            confidence="HIGH",
            formula=(
                f"net_worth − {PRIMARY_FX_SHOCK:.0%} × usd_exposure_nis — "
                "gate fx_shock_-0.10 row"
            ),
        )


def _apply_fi_crossing_year(values):
    """Publish retirement.fi_crossing_year from already-resolved figures.
    Reconciled with the FI margin by construction (the money-math returns the
    current year only when liquid already clears the target). Pending when any
    input is missing — never a guess."""
    from datetime import date as _date
    from argosy.services.fi_crossing import fi_crossing_year
    key = "retirement.fi_crossing_year"

    def _r(k):
        rv = values.get(k)
        return rv.value if (rv and rv.status == "resolved" and rv.value is not None) else None

    liquid = _r("portfolio.liquid_net_worth_nis")
    fi_total = _r("retirement.fi_total_capital_nis")
    real_return = _r("retirement.return_assumption_pct")
    savings = _r("savings.annual_net_nis")
    margin = _r("retirement.fi_margin_signed_nis")
    # margin is REQUIRED (codex impl review): the figure must be reconciled with the
    # margin verdict, so a missing margin -> pending, never an un-reconciled year.
    if None in (liquid, fi_total, real_return, savings, margin):
        values[key] = ResolvedValue.pending(key, "year", "fi_crossing inputs pending")
        return
    cur_year = _date.today().year
    yr = fi_crossing_year(
        liquid_now=float(liquid), fi_total=float(fi_total),
        real_return=float(real_return), annual_real_savings=float(savings),
        current_year=cur_year)
    if yr is None:
        values[key] = ResolvedValue.pending(key, "year", "FI target not reached within horizon")
        return
    # Explicit reconciliation with the resolved margin (codex #2/#4): the math
    # already guarantees this because margin = liquid - fi_total (same basis), but
    # enforce it so a future basis drift fails LOUD instead of shipping a
    # contradiction. margin >= 0 -> current year; margin < 0 -> strictly future.
    if margin is not None:
        if margin >= 0 and yr != cur_year:
            log.warning("fi_crossing.margin_reconcile margin>=0 but yr=%s", yr)
            yr = cur_year
        elif margin < 0 and yr <= cur_year:
            values[key] = ResolvedValue.pending(
                key, "year", "fi_crossing contradicts negative margin")
            return
    values[key] = ResolvedValue(
        key=key, value=float(yr), unit="year", status="resolved",
        source_locator="fi_crossing.fi_crossing_year",
        confidence="HIGH",
        formula="first year FV(liquid, real return, end-of-year real-savings annuity) >= FI total capital")


# IPS sleeve target — bound to the canonical cap-derived constant in
# allocation_plan (Argosy's allocation analysis: the direct target that keeps
# total plan LOOK-THROUGH under the 13% cap), never a hand-typed duplicate.
from argosy.services.allocation_plan import NVDA_TARGET_PCT as _NVDA_TARGET_PCT

_NVDA_IPS_TARGET_W = _NVDA_TARGET_PCT / 100.0


def _resolve_nvda_eligible_now_sh(
    session: "Session", user_id: str, values: dict[str, ResolvedValue]
) -> None:
    """Capital-track-eligible NVDA share count, from the latest tax-sim report.

    Deliberately INDEPENDENT of nvda_weight / nvda_cap_pct / the total book / the
    spine gate: it reads only tax-sim lots (``eligible_shares``), which have no
    causal relationship to the book snapshot or the concentration cap. Must run
    even when weight is pending or the spine gate refuses the book, so a
    pending weight (or a degraded/unvalidated snapshot) does not needlessly
    block this figure too. Pending — never a guess — only when the underlying
    tax-sim lookup itself has nothing to report or errors."""
    k = "concentration.nvda_eligible_now_sh"
    elig = None
    try:
        from argosy.services.tax_simulation_ingest import eligible_shares
        elig = eligible_shares(session, user_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("plan_numeric_resolver.nvda_eligible_failed err=%s", exc)
    if elig is None:
        values[k] = ResolvedValue.pending(k, "shares", "no tax-sim report ingested")
    else:
        values[k] = ResolvedValue(
            key=k, value=int(elig), unit="shares", status="resolved",
            source_locator="tax_simulation_lots (latest report, eligible=OK)",
            agent_report_id=None, confidence="HIGH",
            formula="sum(shares) where Holding Period=OK in the latest tax-sim report",
        )


def _apply_nvda_deconcentration(
    session: "Session", user_id: str, values: dict[str, ResolvedValue]
) -> None:
    """Derive the NVDA deconcentration target/sell-count + the capital-track-eligible
    share count (from the latest tax-sim report), as authoritative values. Pending — never
    a guess — when inputs are missing."""
    keys = ("concentration.nvda_target_sh", "concentration.nvda_sell_sh")
    # nvda_eligible_now_sh is resolved separately, independent of weight/cap/book —
    # see _resolve_nvda_eligible_now_sh docstring. Run it first so it is never
    # collaterally blocked by the early returns below.
    _resolve_nvda_eligible_now_sh(session, user_id, values)
    # The IPS target WEIGHT is a policy constant DISTINCT from the 13% hard cap:
    # the cap is LOOK-THROUGH (counts NVDA inside the index sleeves), so the
    # direct target is CAP-DERIVED by Argosy's allocation analysis to keep total
    # plan look-through under it (re-validated every synthesis). Register it as
    # its own resolved pct so the prose's target traces to a canonical value
    # (and can be placeholdered) without collapsing into the cap.
    # Always present (a constant), even when the share-count derivation is pending.
    values["concentration.nvda_target_pct"] = ResolvedValue(
        key="concentration.nvda_target_pct", value=_NVDA_IPS_TARGET_W, unit="pct",
        status="resolved", source_locator="plan_numeric_resolver._NVDA_IPS_TARGET_W",
        confidence="HIGH",
        formula="IPS single-name direct target (cap-derived: keeps total plan look-through under the 13% concentration cap)",
    )
    w = values.get("concentration.nvda_current_pct")
    cap = values.get("concentration.nvda_cap_pct")
    # Sleeve weight may be status=excluded (NVDA unmanaged) — still derive
    # share counts from absolute NVDA value ÷ TRADEABLE SECURITIES book
    # (same denominator as nvda_concentration_pct / the 13% cap). Never NW.
    nvda_weight: float | None = None
    if w is not None and w.status == "resolved" and w.value is not None:
        nvda_weight = float(w.value)
    elif w is not None and w.status == "excluded":
        from argosy.services.holding_books import implied_nvda_weight_frac
        from argosy.services.holding_books import tradeable_securities_nis_for_user

        tradeable = tradeable_securities_nis_for_user(session, user_id)
        nvda_weight = implied_nvda_weight_frac(
            values, tradeable_securities_nis=tradeable,
        )
    # The cap is NOT an input to target/sell shares — verified by execution
    # (target/sell are identical across cap 0.07/0.12/0.13/0.99; only the
    # optional nvda_cap_breach_x diagnostic depends on cap). Gating this
    # derivation on the cap being resolved was a false dependency: on a
    # Phase-3-only plan amendment run (no concentration agent_reports row) the
    # cap sits pending, and this gate permanently pending'd all three share
    # counts, killing the fact-tokenizer anchors for them. Gate on the WEIGHT
    # only; pass cap through (possibly None) to derive_nvda_deconcentration.
    if nvda_weight is None:
        for k in keys:
            values[k] = ResolvedValue.pending(k, "shares", "nvda weight pending")
        return
    nvda_sh = nvda_px = None
    _book_stale = False
    try:
        from argosy.services.holding_books import (
            TotalBookDegraded,
            load_total_book,
            parse_positions_json,
        )

        snap = _head_snapshot_row(session, user_id)
        if snap is not None and _spine_gate_refuses(session, user_id, snap):
            for k in keys:
                values[k] = ResolvedValue.unavailable(
                    k, "shares",
                    "spine gate: head snapshot not validated (no PASS verdict)",
                )
            return
        raw = parse_positions_json(snap.positions_json if snap else None)
        book = load_total_book(
            session, user_id, raw,
            snapshot_date=getattr(snap, "snapshot_date", None) if snap else None,
            # Real current date, never the snapshot's own — see Sol BLOCK-1.
        )
        if book.degraded:
            for k in keys:
                values[k] = ResolvedValue.unavailable(
                    k, "shares",
                    f"total book degraded — refusing sell-share derivation: "
                    f"{book.degrade_reason}",
                )
            return
        total = book.total
        _book_stale = bool(book.stale_marks)
        for p in total:
            if str(p.get("symbol", "")).upper() == "NVDA":
                nvda_sh = _to_float(p.get("shares"))
                nvda_px = _to_float(p.get("current_price"))
                break
    except TotalBookDegraded as exc:
        for k in keys:
            values[k] = ResolvedValue.unavailable(
                k, "shares", f"total book degraded: {exc.reason}",
            )
        return
    except Exception as exc:  # noqa: BLE001
        log.warning("plan_numeric_resolver.nvda_deconc_snapshot_failed err=%s", exc)
    if not nvda_sh or not nvda_px:
        for k in keys:
            values[k] = ResolvedValue.pending(k, "shares", "no NVDA snapshot position")
        return
    from argosy.services.plan_derivation import derive_nvda_deconcentration

    # cap is resolved-or-None here, never a placeholder — see the false-dependency
    # note above the weight-only gate. derive_nvda_deconcentration treats a None
    # cap by simply omitting the optional nvda_cap_breach_x diagnostic; target/sell
    # are unaffected either way.
    _cap_val = (
        float(cap.value)
        if (cap is not None and cap.status == "resolved" and cap.value is not None)
        else None
    )
    dec = derive_nvda_deconcentration(
        nvda_sh=int(nvda_sh), nvda_px_usd=nvda_px, nvda_weight=float(nvda_weight),
        target_w=_NVDA_IPS_TARGET_W, cap=_cap_val,
    )
    # Actionable sell/target shares can be no more confident than the NVDA
    # weight they derive from (MEDIUM when that weight rode a soft-stale mark)
    # nor the book they read — don't launder a downgrade into HIGH (Sol #3).
    _w_conf = getattr(w, "confidence", "HIGH") or "HIGH"
    _deconf = "HIGH" if (_w_conf == "HIGH" and not _book_stale) else "MEDIUM"
    _deconf_note = (
        " [confidence inherited from a stale/downgraded NVDA weight or book]"
        if _deconf != "HIGH"
        else ""
    )
    for k, field in (("concentration.nvda_target_sh", "nvda_target_sh"),
                     ("concentration.nvda_sell_sh", "nvda_sell_sh")):
        values[k] = ResolvedValue(
            key=k, value=dec[field].value, unit="shares", status="resolved",
            source_locator=(
                f"derive_nvda_deconcentration ({_NVDA_TARGET_PCT:g}% "
                "cap-derived IPS target)" + _deconf_note
            ),
            agent_report_id=None, confidence=_deconf, formula=dec[field].formula,
        )
    # nvda_eligible_now_sh was already resolved at the top of this function,
    # independent of weight/cap/book — see _resolve_nvda_eligible_now_sh.


def _apply_adjudicated_glide(
    session: "Session", user_id: str, values: dict[str, ResolvedValue]
) -> None:
    """Read the SETTLED NVDA glide/vest verdict from the proposal substrate.

    The fleet AUTHORS deconcentration/vest policy (adjudicated glide-schedule
    verdict, ``dedup_key = plan_glide_schedule_verdict:<user>:nvda``, accepted
    by the user); determinism only STATES it. Registers
    ``concentration.nvda_quota_tax_year_sh`` — the CURRENT calendar-tax-year
    sale quota per the settled schedule — with the verdict's
    ``chosen_schedule`` statement carried in ``formula`` so the renderer can
    state the adjudicated policy verbatim. Pending (never a guess, never a
    default policy) when no settled verdict exists.
    """
    from datetime import UTC, datetime

    key = "concentration.nvda_quota_tax_year_sh"
    row = None
    try:
        from argosy.state.models import ActionProposal

        row = session.execute(
            select(ActionProposal)
            .where(
                ActionProposal.user_id == user_id,
                ActionProposal.kind == "update_plan_assumption",
                # Settled = the fleet's adjudicated verdict CONFIRMED by the
                # user (accepted) or already applied (executed). An 'open'
                # verdict is not yet settled — the renderer then states the
                # neutral quota-pace wording instead.
                ActionProposal.status.in_(("accepted", "executed")),
                ActionProposal.dedup_key.like("plan_glide_schedule_verdict:%"),
            )
            .order_by(ActionProposal.id.desc())
            .limit(1)
        ).scalars().first()
    except Exception as exc:  # noqa: BLE001 — degrade to pending, never raise
        log.warning("plan_numeric_resolver.adjudicated_glide_failed err=%s", exc)
    if row is None:
        values[key] = ResolvedValue.pending(
            key, "shares", "no settled glide-schedule verdict on file")
        return
    try:
        payload = json.loads(row.suggested_payload or "{}")
        verdict = payload.get("verdict") if isinstance(payload, dict) else None
        verdict = verdict if isinstance(verdict, dict) else {}
    except (TypeError, ValueError):
        verdict = {}
    schedule = str(verdict.get("chosen_schedule") or "").strip()
    year = datetime.now(UTC).year
    quota = _to_float(verdict.get(f"quota_{year}_shares"))
    locator = f"action_proposals #{row.id} (adjudicated glide verdict, {row.status})"
    if quota is None:
        # Verdict exists but carries no quota for THIS tax year — keep the
        # policy statement (formula) so the renderer still states it.
        values[key] = ResolvedValue.pending(
            key, "shares", f"{locator} — no quota_{year}_shares in payload",
            formula=schedule or None,
        )
        return
    values[key] = ResolvedValue(
        key=key, value=float(quota), unit="shares", status="resolved",
        source_locator=locator, agent_report_id=None, confidence="HIGH",
        # ``formula`` carries the verdict's own policy statement so the
        # renderer states the ADJUDICATED policy verbatim (or falls back to
        # the neutral quota-pace wording when the payload has no statement).
        formula=schedule or None,
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
    # BLOCKER 2 (FM run 379): "the permanent-equivalent FI spend basis is
    # unaudited — needs an itemised derivation (what sums to it) with a cited
    # source." Every line of the sum + its source now lives in the formula
    # string so the synthesizer (and Ariel) can audit it without re-deriving.
    itemized = m.itemized_spend_derivation()
    values["retirement.fi_target_nis"] = ResolvedValue(
        key="retirement.fi_target_nis",
        value=float(m.fi_perpetuity_nis),
        unit="nis",
        status="resolved",
        source_locator="fi_methodology.fi_perpetuity_nis (permanent_spend / SWR)",
        agent_report_id=None,
        confidence=conf,
        formula=f"{m.method} | spend basis derivation: {itemized}",
    )
    values["spend.fi_basis_nis"] = ResolvedValue(
        key="spend.fi_basis_nis",
        value=float(m.permanent_annual_spend_nis),
        unit="nis",
        status="resolved",
        source_locator="fi_methodology.permanent_annual_spend_nis",
        agent_report_id=None,
        confidence=conf,
        formula=(
            "tracked baseline (ex-mortgage) + amortized life-event spend; "
            f"itemized: {itemized}"
        ),
    )
    # BLOCKER 3 (FM run 379): "SWR of 3.0% is not reconciled with the 2.4% real
    # yield the user's resolution used — two different rates in one model."
    # Investigation: there is only ONE rate parameter here — SWR_REAL_CENTRAL_PCT
    # = 3.0% (argosy/services/fi_methodology.py) — sized for the household's
    # explicit no-principal-drawdown mandate over a 90+ year perpetuity (see
    # module docstring). 2.4% is NOT a second, competing rate: it is the LOW
    # (conservative) end of THIS SAME central rate's documented sensitivity band
    # (SWR_REAL_BAND = 2.4%-3.5%), used for stress-testing the SAME perpetuity,
    # not a different governing rate. 3.0% governs the published FI target
    # (fi_perpetuity_nis = permanent_spend / 3.0%) everywhere in this plan; 2.4%
    # only ever appears as the pessimistic edge of that band's sensitivity
    # check. This is DECOUPLED from retirement.return_assumption_pct (5.0%,
    # the trajectory's expected real portfolio RETURN) — that is a third,
    # genuinely different rate used for a different purpose (growing the
    # portfolio forward in time), not for sizing the perpetuity, and is not
    # the "2.4%" the objection refers to.
    values["retirement.required_real_yield_pct"] = ResolvedValue(
        key="retirement.required_real_yield_pct",
        value=float(m.swr_real_pct),
        unit="pct",
        status="resolved",
        source_locator=(
            "fi_methodology.SWR_REAL_CENTRAL_PCT (perpetual real after-tax SWR; "
            "the ONLY SWR used to size fi_target_nis / fi_perpetuity_nis)"
        ),
        agent_report_id=None,
        confidence=conf,
        formula=(
            f"central defensible perpetual real SWR = {m.swr_real_pct*100:.1f}%, "
            f"sized for capital_preservation_returns_only (no-principal-drawdown) "
            f"over a 90+yr horizon, decoupled from the 5.0% expected real RETURN "
            f"(retirement.return_assumption_pct — trajectory growth, NOT perpetuity "
            f"sizing); sensitivity band {m.swr_band[0]*100:.1f}%-{m.swr_band[1]*100:.1f}% "
            f"is the SAME rate's conservative-to-optimistic range, NOT a second rate — "
            f"the {m.swr_band[0]*100:.1f}% low end is what a stress-test of THIS "
            f"perpetuity looks like, never the rate the published target uses"
        ),
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
    # from retirement to the age-60 pension unlock.
    #
    # ARIEL'S RULING (2026-08-18, RED-12/RED-16/Decision 2): fi_age must NEVER
    # size a published FIRE bridge. The two PUBLISHED bridge keys —
    # ``retirement.fire_bridge_nis`` (mandate case, sized from
    # retirement.preservation_age) and ``retirement.fire_bridge_offmandate_nis``
    # (off-mandate case, sized from retirement.earliest_safe_age) — are set
    # ONLY in ``_apply_canonical_dual_track_age`` (the deterministic MC ages),
    # and ONLY on display surfaces that resolve canonical ages
    # (include_canonical_ages=True). They stay PENDING here — never a
    # fi_age-based fabrication under the published key names.
    #
    # ``retirement.fi_age`` remains resolvable as an agent OPINION (see the
    # withdrawal_sequencer role above) with an fi_age-labeled estimate kept
    # under its OWN, clearly-non-published key so any internal caller that
    # wants a cheap (non-MC) approximation can still get one — but it can
    # never masquerade as retirement.fire_bridge_nis.
    bridge_key = "retirement.fire_bridge_nis"
    offmandate_bridge_key = "retirement.fire_bridge_offmandate_nis"
    fi_age_estimate_key = "retirement.fire_bridge_fi_age_estimate_nis"
    fi_age_rv = values.get("retirement.fi_age")
    fi_age = (
        float(fi_age_rv.value)
        if (fi_age_rv is not None and fi_age_rv.status == "resolved" and fi_age_rv.value is not None)
        else None
    )
    if fi_age is not None:
        from argosy.services.cashflow_projection import LUMP_PENSION_AGE
        bridge_years = max(0.0, float(LUMP_PENSION_AGE) - fi_age)
        values[fi_age_estimate_key] = ResolvedValue(
            key=fi_age_estimate_key,
            value=bridge_years * float(m.permanent_annual_spend_nis),
            unit="nis",
            status="resolved",
            source_locator=(
                f"({LUMP_PENSION_AGE} − retirement.fi_age) yrs × "
                "fi_methodology.permanent_annual_spend_nis "
                "[INTERNAL ESTIMATE ONLY — fi_age is an agent opinion, not a "
                "deterministic MC reading; this key must NEVER be presented "
                "as a published bridge figure. See retirement.fire_bridge_nis "
                "(mandate case) / retirement.fire_bridge_offmandate_nis "
                "(off-mandate case) for the published figures.]"
            ),
            agent_report_id=None,
            confidence=conf,
            formula="NON-PUBLISHED internal estimate from the fi_age agent-opinion trajectory marker — never sized against a published age, never rendered as the FIRE bridge",
        )
    values.setdefault(
        bridge_key,
        ResolvedValue.pending(
            bridge_key, "nis",
            "needs retirement.preservation_age (canonical ages; include_canonical_ages=True)",
        ),
    )
    values.setdefault(
        offmandate_bridge_key,
        ResolvedValue.pending(
            offmandate_bridge_key, "nis",
            "needs retirement.earliest_safe_age (canonical ages; include_canonical_ages=True)",
        ),
    )


# ---------------------------------------------------------------------------
# Synth-prompt rendering — feed the derived headline numbers INTO the
# synthesizer so it consumes them rather than authoring its own.
# ---------------------------------------------------------------------------

# Display order + human labels for the headline numbers the synthesizer is
# allowed to state. Pending keys still render (as [derivation pending]) so the
# model knows the figure exists but has no approved value.
_SYNTH_DISPLAY: tuple[tuple[str, str], ...] = (
    ("portfolio.liquid_net_worth_nis", "Liquid net worth (spendable; EXCLUDES all real estate — THE FI sufficiency basis)"),
    ("portfolio.net_worth_nis", "Investable net worth (incl. foreign real-estate row; NOT the FI basis — reconciliation only)"),
    ("retirement.fi_target_nis", "FI capital target (perpetuity)"),
    ("retirement.fi_total_capital_nis", "FI total capital target (perpetuity + reserve)"),
    ("retirement.fi_margin_signed_nis", "FI sufficiency margin GROSS (LIQUID net worth − total target; >0 => reached on a gross basis; if <0, FI is NOT reached even gross — do not claim funded)"),
    ("retirement.fi_margin_net_of_realization_nis", "FI sufficiency margin NET OF REALIZATION TAX — FULL-LIQUIDATION BOUND (gross margin − tax on ALL 10,940 NVDA shares; >0 => FI reached even if the entire position were sold today; <0 => FI is NOT reached under that bound; this is a conservative BOUND, not the plan — see the glide-consistent figure below for the plan as written)"),
    ("retirement.fi_margin_net_of_realization_glide_nis", "FI sufficiency margin NET OF REALIZATION TAX — PLAN AS WRITTEN (gross margin − tax on ONLY the planned sale, concentration.nvda_sell_sh shares, eligible/breaking split respected; retained shares untaxed; >0 => the plan as written clears FI after tax; <0 => it does not — cite THIS one, not the full-liquidation bound, when describing what the plan actually does)"),
    ("tax.nvda_embedded_cgt_nis", "NVDA embedded Israeli tax liability — FULL LIQUIDATION (ALL 10,940 shares; §102 Capital 30% on eligible lots + per-lot implied rate on breaking lots; REVALUED to current NVDA price where available — cite the price and sim date from source_locator). This is an upper BOUND, not the plan — the deconcentration glide retains concentration.nvda_target_sh shares."),
    ("tax.nvda_embedded_cgt_glide_nis", "NVDA embedded Israeli tax liability — PLANNED SALE ONLY (concentration.nvda_sell_sh shares; capital-track eligible shares at §102 30%, the remainder at Breaking/ordinary rate, never blended; single-tax-year assumption — see source_locator)"),
    ("tax.nvda_net_proceeds_nis", "NVDA net after-tax proceeds (full position; what survives all Israeli §102 + surtax; REVALUED to current price where available)"),
    ("retirement.fi_shock_net_worth_nis", "Net worth after −30% NVDA shock (gate shock_0.30; cite this — never invent a shocked NW)"),
    ("retirement.fi_fx_shock_net_worth_nis", "Net worth after −10% adverse FX move on USD exposure (gate fx_shock_-0.10; cite this — never invent)"),
    # The sleeve pct is interpolated from the engine constant so these labels
    # can never drift from the cap-derived target (the "12% sleeve ghost").
    # NOTE: keep the word "cap" OUT of these labels — the pace fallback's
    # stock-target skip list treats cap-labelled rows as ceilings, not flows.
    ("concentration.nvda_target_sh", f"NVDA target shares (≤{_NVDA_TARGET_PCT:g}% IPS sleeve — the DERIVED deconcentration target; replaces any inherited cadence)"),
    ("concentration.nvda_sell_sh", f"NVDA shares to SELL to reach the {_NVDA_TARGET_PCT:g}% target (derived; capital-track-eligible count below gates the pace)"),
    ("concentration.nvda_eligible_now_sh", "NVDA shares already Section-102 capital-track eligible NOW (~25%; from the tax-sim report)"),
    ("retirement.liquidity_reserve_nis", "Liquidity reserve (finite liabilities, held separately)"),
    ("retirement.fire_bridge_nis", "FIRE bridge — MANDATE case (retirement.preservation_age → age-60 unlock, liquid drawdown, permanent-equivalent). Publish alongside retirement.fire_bridge_offmandate_nis; never alone as 'the' bridge."),
    ("retirement.fire_bridge_offmandate_nis", "FIRE bridge — OFF-MANDATE case (retirement.earliest_safe_age → age-60 unlock, liquid drawdown, permanent-equivalent). Publish alongside retirement.fire_bridge_nis; never alone."),
    ("concentration.us_situs_estate_exposure_nis", "US-situs estate exposure (IRS NRA — all US-domiciled securities, every broker)"),
    ("spend.fi_basis_nis", "FI spend basis (permanent-equivalent, real)"),
    ("retirement.required_real_yield_pct", "Required real yield (perpetual safe-withdrawal rate)"),
    ("retirement.return_assumption_pct", "Expected real return (trajectory only)"),
    ("retirement.preservation_age", "MANDATE-SATISFYING retirement age — capital-preservation / no-principal-drawdown reading (worst-10% real path preserves today's real principal to 95, ≥99% MC-solvent). Matches the household's EXPLICIT mandate (goals_yaml.retirement_drawdown_style=capital_preservation_returns_only: 'no principal drawdown ... 0% SWR on principal'). ALWAYS state alongside retirement.earliest_safe_age — NEITHER age is 'the' retirement age on its own; if only one can be shown, show THIS one."),
    ("retirement.earliest_safe_age", "OFF-MANDATE retirement age — typical Monte-Carlo drawdown, 90% solvency to 95, PERMITS spending principal. NOT the household's stated mandate. ALWAYS state alongside retirement.preservation_age — never present this age alone as 'the' retirement age."),
    ("retirement.drawdown_scenario_age", "Duplicate key for retirement.earliest_safe_age (same off-mandate value) — kept for surfaces already wired to this name."),
    ("retirement.fi_age", "Agent-OPINION trajectory marker (withdrawal_sequencer LLM output, may be donor-inherited/stale) — NOT a deterministic MC reading of either policy. MUST NEVER be presented as a retirement age or used to size a published FIRE bridge (see retirement.preservation_age / retirement.earliest_safe_age / retirement.fire_bridge_nis / retirement.fire_bridge_offmandate_nis)."),
    ("retirement.pension_unlock_age", "Pension/hishtalmut unlock age (FIRE-bridge endpoint — a fixed constant)"),
    ("retirement.mc_horizon_age", "Monte-Carlo solvency horizon age (every drawdown P(ruin) runs to here — a fixed constant)"),
    ("spend.annual_t12_nis", "Current tracked spend (T12)"),
    ("savings.annual_net_nis", "Annual net savings (RSU, conservative floor)"),
    ("concentration.nvda_target_pct", "NVDA IPS target weight (the steering target — DISTINCT from the cap below)"),
    ("concentration.nvda_cap_pct", "NVDA concentration cap (the hard ceiling — ~1pp above the target)"),
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
    if rv.unit == "shares":
        return f"{v:,.0f} sh"
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
    # Fact-placeholder protocol (default ON — ARGOSY_FACT_PLACEHOLDERS=0 to
    # kill). When on, the synthesizer must EMIT the {{fact:key}} token
    # verbatim wherever it would state a headline figure, instead of typing
    # the digits. READ-time rendering fills the token from the live manifest
    # so a trade updates numbers without rewriting the plan.
    import os as _os
    from argosy.quality.fact_registry import FACT_DISPLAY as _FACT_DISPLAY

    def _placeholders_on() -> bool:
        env = _os.environ.get("ARGOSY_FACT_PLACEHOLDERS")
        if env is not None:
            return str(env).strip().lower() in {"1", "true", "yes", "on"}
        try:
            from argosy.config import get_settings
            return bool(get_settings().fact_placeholders)
        except Exception:  # noqa: BLE001
            return True

    if _placeholders_on():
        lines.append(
            "PLACEHOLDER PROTOCOL (MANDATORY): for every headline figure below, "
            "write its `{{fact:<key>}}` token VERBATIM in the plan body instead of "
            "typing the number. Do NOT type the digits for these facts — a renderer "
            "substitutes the canonical value at READ time and a gate rejects a "
            "hand-typed headline number that has a matching fact key. Narrative "
            "numbers that are NOT in this list (e.g. a 30% surtax rate, 90% MC "
            "solvency) are typed normally."
        )
        lines.append("")
    for key, label in _SYNTH_DISPLAY:
        rv = resolved.get(key)
        disp = _display_value(rv)
        src = rv.source_locator if rv.status == "resolved" else "no approved source"
        conf = f"; conf {rv.confidence}" if rv.confidence else ""
        if _placeholders_on() and key in _FACT_DISPLAY and rv.status == "resolved":
            # Present the token as bracketed metadata, NOT as a copyable "EMIT AS:"
            # instruction — the model was reproducing that verb verbatim in prose. A
            # render-time sanitizer (fact_registry.strip_emission_scaffolding) strips any
            # residual leak as a deterministic backstop.
            lines.append(
                f"  - {label}: {disp}   [write the token {{{{fact:{key}}}}} verbatim · {src}{conf}]"
            )
        else:
            lines.append(f"  - {label}: {disp}   [{src}{conf}]")

    # Canonical FI-sufficiency VERDICT — a single rendered conclusion the synthesizer must
    # state VERBATIM. The recurring contradiction was the model RE-COMPUTING sufficiency
    # from net worth − target itself (getting +118,020 off the investable basis) while the
    # canonical margin is −148,208 on the liquid basis. Render the conclusion, don't let it
    # be generated.
    #
    # QUALIFICATION RULE (blocker 1 fix): when the gross margin is positive but the
    # net-of-realization margin is negative, an unqualified "REACHED" verdict is wrong in
    # the direction that flatters Ariel.  The verdict MUST carry the net basis explicitly
    # so no surface can state reached-ness without stating on which basis.
    margin = resolved.get("retirement.fi_margin_signed_nis")
    net_margin_rv = resolved.get("retirement.fi_margin_net_of_realization_nis")
    if margin is not None and margin.status == "resolved" and margin.value is not None:
        m = float(margin.value)
        net_m: float | None = (
            float(net_margin_rv.value)
            if (
                net_margin_rv is not None
                and net_margin_rv.status == "resolved"
                and net_margin_rv.value is not None
            )
            else None
        )
        if m >= 0:
            if net_m is not None and net_m < 0:
                # Gross positive, net negative — the most important case to qualify.
                # Do NOT say "REACHED" without the gross-only qualifier.
                verdict = (
                    f"FI sufficiency VERDICT: REACHED ON A GROSS PRE-TAX BASIS ONLY — "
                    f"liquid net worth exceeds the total capital target by ₪{m:,.0f} BEFORE "
                    f"accounting for the embedded NVDA realization tax. "
                    f"Net of realization tax, the margin is ₪{net_m:,.0f} (a SHORTFALL). "
                    f"FI is NOT reached on an after-tax basis. "
                    f"You MUST state both bases; do NOT write 'FI reached' without the "
                    f"gross-only qualifier."
                )
            elif net_m is not None and net_m >= 0:
                # Both gross and net positive — full statement.
                verdict = (
                    f"FI sufficiency VERDICT: REACHED — liquid net worth covers the "
                    f"total capital target with a ₪{m:,.0f} gross margin and a "
                    f"₪{net_m:,.0f} net-of-realization-tax margin (both positive)."
                )
            else:
                # Net margin pending — report gross only, note net is pending.
                verdict = (
                    f"FI sufficiency VERDICT: REACHED on a gross basis — liquid net worth "
                    f"covers the total capital target with a ₪{m:,.0f} margin. "
                    f"Net-of-realization-tax margin: [derivation pending] — do NOT claim "
                    f"FI is fully funded until the after-tax figure is resolved."
                )
        else:
            verdict = (
                f"FI sufficiency VERDICT: NOT reached — liquid net worth is short "
                f"₪{abs(m):,.0f} of the total capital target on a gross basis."
            )
            if net_m is not None:
                verdict += (
                    f" Net-of-realization-tax margin: ₪{net_m:,.0f} "
                    f"({'also a shortfall' if net_m < 0 else 'positive post-tax — unusual'})."
                )
            verdict += " Do NOT state FI is funded/reached anywhere."
        lines += [
            "",
            verdict,
            "  ^ State FI sufficiency ONLY via this VERDICT. You are FORBIDDEN from "
            "computing or stating any OTHER sufficiency margin (e.g. subtracting the "
            "target from investable net worth to get a different/positive number) — that "
            "is the recurring cross-surface contradiction. Liquid is the only FI basis; "
            "never label investable net worth as 'liquid'.",
        ]

    # Canonical RETIREMENT-AGE verdict. ARIEL'S RULING (2026-08-18): publish
    # BOTH ages, no single headline. The recurring contradiction used to be
    # three age concepts (earliest-safe, fi_age trajectory marker, "crosses
    # target in year N") conflated into one client-facing number; the fix is
    # NOT to pick a winner — it is to always state the pair with its
    # trade-off, and to keep fi_age out of the published set entirely.
    pres = resolved.get("retirement.preservation_age")
    esa = resolved.get("retirement.earliest_safe_age")
    pres_ok = pres is not None and pres.status == "resolved" and pres.value is not None
    esa_ok = esa is not None and esa.status == "resolved" and esa.value is not None
    if pres_ok and esa_ok:
        age_verdict = (
            f"RETIREMENT-AGE VERDICT: publish BOTH ages, never one alone — "
            f"{float(pres.value):.0f} under capital-preservation / no-principal-"
            f"drawdown (the household's EXPLICIT mandate: worst-10% real path "
            f"preserves today's real principal to 95, ≥99% MC-solvent), or "
            f"{float(esa.value):.0f} if principal drawdown is permitted (typical "
            f"Monte Carlo, 90% solvency to 95 — OFF-MANDATE). State the pair "
            f"together every time a retirement age is stated. If a surface can "
            f"show only one number, show {float(pres.value):.0f} (the mandate case) "
            f"and say the off-mandate {float(esa.value):.0f} exists."
        )
    elif pres_ok or esa_ok:
        only = pres if pres_ok else esa
        label = "mandate (capital-preservation)" if pres_ok else "off-mandate (drawdown)"
        age_verdict = (
            f"RETIREMENT-AGE VERDICT: only the {label} age is resolved "
            f"({float(only.value):.0f}); the other is [derivation pending]. Do NOT "
            "state a single retirement age as if it were the whole picture — say "
            "the other reading is pending, not that this is 'the' age."
        )
    else:
        age_verdict = ("RETIREMENT-AGE VERDICT: both retirement ages are "
                       "[derivation pending] — do NOT state ANY retirement age; "
                       "write [derivation pending].")
    lines += [
        "",
        age_verdict,
        "  ^ fi_age (agent-OPINION trajectory marker — withdrawal_sequencer LLM "
        "output, may be donor-inherited/stale) and any 'portfolio crosses the FI "
        "target in year N / today' projection are NOT a retirement age — NEVER "
        "present them as 'you can retire at X'. Every age-bearing surface must "
        "state the SAME pair as this VERDICT. The FIRE bridge is published as TWO "
        "figures: retirement.fire_bridge_nis (sized from retirement.preservation_age, "
        "the mandate case) and retirement.fire_bridge_offmandate_nis (sized from "
        "retirement.earliest_safe_age, the off-mandate case). Neither bridge figure "
        "is ever sized from fi_age.",
    ]

    # RSU-VEST-POLICY block — FACTS + the ADJUDICATED policy only. HISTORY:
    # this block once HARDCODED a sell-net-vested-at-vest directive (the
    # anti-'3,000 sh/yr laundering' guard), which let a deterministic renderer
    # AUTHOR policy the fleet had since adjudicated the other way (glide
    # verdict: capital-rate-only sales; fresh vests season on their own §102
    # clock). Doctrine: the fleet AUTHORS policy; determinism STATES FACTS and
    # carries the SETTLED verdict — it never instructs a vest policy of its own.
    w = resolved.get("concentration.nvda_current_pct")
    elig = resolved.get("concentration.nvda_eligible_now_sh")
    quota = resolved.get("concentration.nvda_quota_tax_year_sh")
    # Weight may be status=excluded (NVDA unmanaged): still state the FACTS
    # using absolute NVDA value ÷ net worth when available.
    weight_frac: float | None = None
    weight_note = ""
    if w is not None and w.status == "resolved" and w.value is not None:
        weight_frac = float(w.value)
    elif w is not None and w.status == "excluded":
        nvda_val = resolved.get("concentration.nvda_value_nis")
        nw = resolved.get("portfolio.net_worth_nis")
        if (
            nvda_val is not None and nvda_val.status == "resolved"
            and nw is not None and nw.status == "resolved"
            and nvda_val.value is not None and nw.value is not None
            and float(nw.value) > 0
        ):
            weight_frac = float(nvda_val.value) / float(nw.value)
            weight_note = " (TOTAL book; excluded from sleeve-weight reporting)"
    if weight_frac is not None and weight_frac > _NVDA_IPS_TARGET_W:
        from datetime import UTC, datetime

        elig_txt = (f"{int(elig.value):,} NVDA shares are Section-102 "
                    "capital-track eligible NOW (the sellable pool)"
                    if elig is not None and elig.status == "resolved" and elig.value is not None
                    else "the capital-track-eligible pool is in the ingested tax-sim report")
        lines += [
            "",
            "RSU-VEST-POLICY FACTS (state these; the vest/sale POLICY itself is "
            "fleet-authored — see the adjudicated policy below):",
            f"  - Concentration: NVDA is {weight_frac * 100:.0f}% of the book"
            f"{weight_note} vs the "
            f"{_NVDA_IPS_TARGET_W:.0%} IPS target.",
            f"  - Eligible pool: {elig_txt}.",
            "  - Per-lot Section-102 clock rule: each lot becomes capital-track "
            "eligible only after ITS OWN holding period — per-lot eligibility "
            "governs which lots may be sold; selling a lot before its clock "
            "(e.g. a fresh vest) is taxed as ordinary income (up to ~50%), not "
            "capital gains.",
            "  - Anti-laundering: the sale quota derives from the ADJUDICATED "
            "glide schedule, never from prior-year sale cadence (a past-behavior "
            "'shares/yr' figure is history, not policy).",
        ]
        if quota is not None and quota.status == "resolved" and quota.value is not None:
            yr = datetime.now(UTC).year
            lines.append(
                f"  - Tax-year sale quota ({yr}, per the adjudicated glide): "
                f"{int(quota.value):,} shares.   [{quota.source_locator}]"
            )
        settled_policy = (quota.formula or "").strip() if quota is not None else ""
        if settled_policy:
            lines += [
                f"  ADJUDICATED VEST/SALE POLICY (settled fleet verdict — state it "
                f"verbatim, do not re-decide): {settled_policy}",
                "  ^ State ONE vest/sale policy consistent with this adjudicated "
                "verdict and the per-lot clock rule above. Do NOT author any "
                "vest-timing policy the fleet has not adjudicated.",
            ]
        else:
            lines += [
                "  POLICY (no settled adjudication on file): sales proceed at the "
                "adjudicated quota pace from capital-track-eligible lots; per-lot "
                "eligibility governs which lots may be sold. Do NOT author any "
                "vest-timing policy the fleet has not adjudicated.",
            ]
    return "\n".join(lines)


__all__ = [
    "ResolvedValue",
    "ResolvedPlanNumbers",
    "resolve_plan_numbers",
    "render_numbers_for_synth",
    "PENDING_LABEL",
]
