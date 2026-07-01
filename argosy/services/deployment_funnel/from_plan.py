"""Adapt a `DeploymentPlan` (candidate generator output) + the user's plan doc
and holdings into a deterministic preflight run. This is the glue Task 7 wires
into `GET /api/portfolio/deploy-cash` behind the kill switch."""
from __future__ import annotations

import asyncio
import logging
from typing import Iterable

from argosy.services.contracts import AllocationCandidate, AllocationLeg
from argosy.services.deployment_funnel.contracts import PreflightResult
from argosy.services.deployment_funnel.gates import GateInputs
from argosy.services.deployment_funnel.look_through import effective_nvda_usd
from argosy.services.deployment_funnel.preflight import run_preflight
from argosy.services.deployment_funnel.reserve import (
    CASH_LIKE_SYMBOLS,
    existing_cash_like_usd,
    reserve_shortfall_usd,
)

_log = logging.getLogger(__name__)

# The plan class that represents the cash/T-bill reserve (matched by label
# substring so a rename to "Cash & T-bills (incl. ILS tranche)" still resolves).
_RESERVE_LABEL_HINT = "cash & t-bills"


def build_gate_inputs(*, doc, holdings_usd: dict[str, float], cash_usd: float) -> GateInputs:
    """Assemble the deterministic gate inputs from the accepted plan doc + the
    latest holdings. Effective NVDA is the look-through sum over current
    holdings; the reserve target is the plan's cash/T-bills class weight."""
    book_usd = round(sum(holdings_usd.values()) + cash_usd, 2)

    current_nvda = 0.0
    for sym, val in holdings_usd.items():
        current_nvda += effective_nvda_usd(sym, val)
    current_nvda = round(current_nvda, 2)

    plan_classes = frozenset(c.label for c in doc.classes)
    class_of: dict[str, str] = {}
    for c in doc.classes:
        for instr in c.instruments:
            class_of[instr.symbol.upper()] = c.label

    reserve_pct = 0.0
    for c in doc.classes:
        if _RESERVE_LABEL_HINT in c.label.lower():
            reserve_pct += float(c.target_pct)

    # Cash sits outside `holdings_usd` (tradeable_holdings pulls it out), so add
    # it back as a cash-like row for the reserve calculation.
    holdings_for_reserve = dict(holdings_usd)
    holdings_for_reserve["CASH_USD"] = holdings_for_reserve.get("CASH_USD", 0.0) + cash_usd

    shortfall = reserve_shortfall_usd(book_usd, holdings_for_reserve, reserve_pct)

    return GateInputs(
        current_effective_nvda_usd=current_nvda,
        book_usd=book_usd,
        nvda_cap_pct=float(doc.nvda_cap_pct),
        reserve_shortfall_usd=shortfall,
        plan_classes=plan_classes,
        class_of=class_of,
    )


def plan_to_candidates(plan) -> list[AllocationCandidate]:
    """Flatten a DeploymentPlan's tiers into BUY candidates (one leg each)."""
    out: list[AllocationCandidate] = []
    for tier in plan.tiers:
        for line in tier.lines:
            out.append(
                AllocationCandidate(
                    kind="BUY",
                    legs=(
                        AllocationLeg(
                            side="BUY", symbol=line.symbol, account_id="leumi",
                            currency="USD", notional_usd=float(line.amount_usd),
                            funding_source="cash",
                        ),
                    ),
                    horizon="now",
                    rationale=line.rationale,
                )
            )
    return out


class SnapshotOrLiveProvider:
    """Best-effort price provider. Prefers a live quote from the yfinance
    adapter; falls back to a passed-in snapshot price map. A symbol with neither
    yields ``None`` (the gates then DEFER — fail-closed, never act blind).
    History high / z-score are left to Increment 2's EOD enrichment (``None``
    here does NOT mark the candidate stale — only a missing last price does)."""

    def __init__(self, snapshot_prices: dict[str, float] | None = None):
        self._snap = {k.upper(): v for k, v in (snapshot_prices or {}).items()}
        self._live: dict[str, float | None] = {}

    def _live_quote(self, symbol: str) -> float | None:
        if symbol in self._live:
            return self._live[symbol]
        price: float | None = None
        try:
            from argosy.adapters.data.yfinance_adapter import YFinanceAdapter

            q = asyncio.run(YFinanceAdapter().get_quote(symbol))
            price = float(getattr(q, "price", None)) if q is not None else None
        except Exception as exc:  # noqa: BLE001 — best-effort; stale => defer
            _log.info("deploy_funnel.quote_miss", extra={"symbol": symbol, "err": str(exc)})
            price = None
        self._live[symbol] = price
        return price

    def quote(self, symbol: str) -> float | None:
        # Snapshot price is authoritative for symbols we already hold (recent,
        # no network); fall back to a live fetch only for genuinely new symbols.
        s = symbol.upper()
        if s in self._snap:
            return self._snap[s]
        return self._live_quote(s)

    def history_high(self, symbol: str) -> float | None:
        return None

    def zscore(self, symbol: str) -> float | None:
        return None


def run_preflight_for_plan(
    plan,
    *,
    doc,
    holdings_usd: dict[str, float],
    cash_usd: float,
    deployable_usd: float,
    signals_by_symbol: dict[str, str] | None = None,
    snapshot_prices: dict[str, float] | None = None,
) -> PreflightResult:
    """End-to-end: plan candidates -> gate inputs -> deterministic preflight."""
    gi = build_gate_inputs(doc=doc, holdings_usd=holdings_usd, cash_usd=cash_usd)
    candidates = plan_to_candidates(plan)
    return run_preflight(
        candidates,
        symbol_of=lambda c: c.legs[0].symbol,
        gate_inputs=gi,
        provider=SnapshotOrLiveProvider(snapshot_prices),
        signals_by_symbol=signals_by_symbol or {},
        deployable_usd=deployable_usd,
    )


__all__ = [
    "build_gate_inputs",
    "plan_to_candidates",
    "SnapshotOrLiveProvider",
    "run_preflight_for_plan",
]
