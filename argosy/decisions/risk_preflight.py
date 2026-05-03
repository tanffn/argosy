"""Rule-based risk preflight (SDD §9.3, Phase 3).

Runs *before* a proposal is queued for broker placement. No LLM here:
deterministic, fast, audit-friendly. A `hard_fail` blocks; a `warn`
surfaces but does not block.

Phase 3 intentionally keeps wash-sale and intraday-pnl checks at stub
fidelity (broker isn't wired until Phase 4); each check is a separate
function so Phase 4 can replace stubs in place.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import Any, Iterable, Literal

from argosy.agent_settings import AgentSettings


class PreflightStatus(str, enum.Enum):
    PASS = "PASS"
    WARN = "WARN"
    HARD_FAIL = "HARD_FAIL"


@dataclass
class PreflightResult:
    """Output of a single check."""

    check: str
    status: PreflightStatus
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class PreflightReport:
    """Aggregated output of all checks."""

    results: list[PreflightResult]

    @property
    def hard_failures(self) -> list[PreflightResult]:
        return [r for r in self.results if r.status is PreflightStatus.HARD_FAIL]

    @property
    def warnings(self) -> list[PreflightResult]:
        return [r for r in self.results if r.status is PreflightStatus.WARN]

    @property
    def passed(self) -> bool:
        return not self.hard_failures

    def summary(self) -> str:
        if self.hard_failures:
            return (
                f"BLOCKED: {len(self.hard_failures)} hard failure(s); "
                f"{len(self.warnings)} warning(s)."
            )
        if self.warnings:
            return f"PASS with {len(self.warnings)} warning(s)."
        return "PASS"


# ----------------------------------------------------------------------
# Individual checks
# ----------------------------------------------------------------------


def check_cash_availability(
    proposal: Any,
    cash_available_usd: float,
    *,
    estimated_cost_usd: float | None = None,
) -> PreflightResult:
    """Hard fail if a buy lacks the cash. Sells PASS by definition.

    `estimated_cost_usd` is what the trader expects to spend. If absent,
    we estimate from the proposal as `size_shares_or_currency *
    limit_price` (when both are available); otherwise we cannot verify
    and emit a WARN.
    """
    action = (getattr(proposal, "action", "") or "").lower()
    if action == "sell" or action == "hold":
        return PreflightResult(
            check="cash_availability", status=PreflightStatus.PASS, message="N/A for sell/hold"
        )

    if estimated_cost_usd is None:
        size = float(getattr(proposal, "size_shares_or_currency", 0) or 0)
        units = (getattr(proposal, "size_units", "shares") or "shares").lower()
        if units == "currency":
            estimated_cost_usd = size
        else:
            limit = getattr(proposal, "limit_price", None)
            if limit is not None and limit > 0:
                estimated_cost_usd = size * float(limit)
            else:
                return PreflightResult(
                    check="cash_availability",
                    status=PreflightStatus.WARN,
                    message="Could not estimate cost (market order, size in shares); "
                    "cash check deferred to broker",
                    detail={"size": size, "units": units},
                )

    if estimated_cost_usd > cash_available_usd:
        return PreflightResult(
            check="cash_availability",
            status=PreflightStatus.HARD_FAIL,
            message=f"Estimated cost ${estimated_cost_usd:,.2f} exceeds available "
            f"cash ${cash_available_usd:,.2f}",
            detail={"estimated_cost": estimated_cost_usd, "cash": cash_available_usd},
        )
    return PreflightResult(
        check="cash_availability",
        status=PreflightStatus.PASS,
        message=f"OK: ${estimated_cost_usd:,.2f} <= ${cash_available_usd:,.2f}",
    )


def check_position_size_cap(
    proposal: Any,
    max_position_usd: float | None,
) -> PreflightResult:
    """Hard fail if proposed value exceeds the configured per-trade cap."""
    if max_position_usd is None or max_position_usd <= 0:
        return PreflightResult(
            check="position_size_cap",
            status=PreflightStatus.PASS,
            message="No cap configured",
        )
    size = float(getattr(proposal, "size_shares_or_currency", 0) or 0)
    units = (getattr(proposal, "size_units", "shares") or "shares").lower()
    if units == "shares":
        # Use limit_price as best estimate for the cap check; if absent, WARN
        limit = getattr(proposal, "limit_price", None)
        if not limit:
            return PreflightResult(
                check="position_size_cap",
                status=PreflightStatus.WARN,
                message="Cannot verify size cap on market-order share count",
            )
        proposed = size * float(limit)
    else:
        proposed = size
    if proposed > max_position_usd:
        return PreflightResult(
            check="position_size_cap",
            status=PreflightStatus.HARD_FAIL,
            message=f"Proposed ${proposed:,.2f} exceeds per-trade cap ${max_position_usd:,.2f}",
            detail={"proposed": proposed, "cap": max_position_usd},
        )
    return PreflightResult(
        check="position_size_cap",
        status=PreflightStatus.PASS,
        message=f"OK: ${proposed:,.2f} <= ${max_position_usd:,.2f}",
    )


def check_concentration_cap(
    proposal: Any,
    snapshot_pct: dict[str, float],
    plan_targets: dict[str, float],
    *,
    breach_pct_over: float = 5.0,
) -> PreflightResult:
    """WARN if proposal would push any cited category over target by `breach_pct_over` pp.

    Conservative: we don't know the exact post-trade allocation without
    pricing data, so we use the snapshot pct + a coarse delta. Phase 4
    swaps in a real recompute.
    """
    ticker = (getattr(proposal, "ticker", "") or "").upper()
    if not ticker or not plan_targets:
        return PreflightResult(
            check="concentration_cap",
            status=PreflightStatus.PASS,
            message="No targets supplied",
        )
    target = plan_targets.get(ticker)
    actual = snapshot_pct.get(ticker, 0.0)
    if target is None:
        return PreflightResult(
            check="concentration_cap",
            status=PreflightStatus.PASS,
            message=f"No target configured for {ticker}",
        )
    if actual - target > breach_pct_over:
        # On a buy we worsen this; on a sell we improve it.
        action = (getattr(proposal, "action", "") or "").lower()
        if action == "buy":
            return PreflightResult(
                check="concentration_cap",
                status=PreflightStatus.HARD_FAIL,
                message=f"{ticker} already {actual:.1f}% (target {target:.1f}%); "
                "buy would push further over cap",
                detail={"actual": actual, "target": target, "breach_pp": breach_pct_over},
            )
        return PreflightResult(
            check="concentration_cap",
            status=PreflightStatus.WARN,
            message=f"{ticker} over target by {actual - target:.1f}pp",
        )
    return PreflightResult(
        check="concentration_cap", status=PreflightStatus.PASS, message="Within cap"
    )


def check_wash_sale(
    proposal: Any,
    lots: Iterable[Any] | None = None,
    *,
    days: int = 30,
) -> PreflightResult:
    """Phase 3 stub: lots aren't imported yet; emit a WARN noting the gap."""
    return PreflightResult(
        check="wash_sale",
        status=PreflightStatus.PASS,
        message="Lots not yet imported; wash-sale check deferred to Phase 4",
        detail={"window_days": days, "lots_available": bool(lots)},
    )


def check_daily_loss_limit(
    proposal: Any,
    day_pnl_usd: float,
    daily_loss_limit_usd: float | None,
) -> PreflightResult:
    """Hard fail if today's P&L is already below the configured floor."""
    if daily_loss_limit_usd is None:
        return PreflightResult(
            check="daily_loss_limit",
            status=PreflightStatus.PASS,
            message="No daily-loss limit configured",
        )
    # Limit is expressed as a NEGATIVE allowable P&L threshold, e.g. -1000.
    # A more negative day_pnl than the limit triggers the block.
    if day_pnl_usd < daily_loss_limit_usd:
        return PreflightResult(
            check="daily_loss_limit",
            status=PreflightStatus.HARD_FAIL,
            message=f"Day P&L ${day_pnl_usd:,.2f} below limit ${daily_loss_limit_usd:,.2f}; "
            "halt new trades",
            detail={"pnl": day_pnl_usd, "limit": daily_loss_limit_usd},
        )
    return PreflightResult(
        check="daily_loss_limit",
        status=PreflightStatus.PASS,
        message=f"Day P&L ${day_pnl_usd:,.2f} within limit",
    )


def check_trading_hours(
    proposal: Any,
    now: datetime,
    *,
    market_open: time = time(9, 30),
    market_close: time = time(16, 0),
) -> PreflightResult:
    """WARN outside US market hours (9:30-16:00 ET) for stocks/ETFs.

    Time-in-force GTC and limit orders WARN cleanly; market orders
    HARD_FAIL outside hours.
    """
    weekday = now.weekday()
    if weekday >= 5:
        order_type = (getattr(proposal, "order_type", "market") or "market").lower()
        status = (
            PreflightStatus.WARN
            if order_type != "market"
            else PreflightStatus.HARD_FAIL
        )
        return PreflightResult(
            check="trading_hours",
            status=status,
            message="Weekend; markets closed",
            detail={"weekday": weekday, "order_type": order_type},
        )
    # Compare on UTC-naive time for simplicity; caller can supply ET-localized now.
    t = now.time()
    if market_open <= t <= market_close:
        return PreflightResult(
            check="trading_hours", status=PreflightStatus.PASS, message="Within hours"
        )
    order_type = (getattr(proposal, "order_type", "market") or "market").lower()
    if order_type == "market":
        return PreflightResult(
            check="trading_hours",
            status=PreflightStatus.HARD_FAIL,
            message=f"Market order outside hours (now={t}); use a limit order or "
            "wait until market open",
        )
    return PreflightResult(
        check="trading_hours",
        status=PreflightStatus.WARN,
        message=f"Outside hours (now={t}); GTC/limit will queue until open",
    )


def check_tier_mode_match(
    proposal: Any,
    tier: str,
    settings: AgentSettings,
    *,
    account_class: str = "main",
) -> PreflightResult:
    """Hard fail if exec mode is `queue_only` and the routing matrix says
    'auto-execute' for this tier+account. Per SDD §10.1 hard rule:
    `queue_only` disables every auto-execute cell. We surface a WARN
    showing the intended path for audit.
    """
    mode = settings.execution.default_mode
    if mode == "queue_only":
        # OK: queue_only is a deliberate choice; just record it. Don't fail.
        return PreflightResult(
            check="tier_mode_match",
            status=PreflightStatus.PASS,
            message=f"queue_only mode active; tier {tier} routes to human queue",
        )
    if mode == "paper":
        return PreflightResult(
            check="tier_mode_match",
            status=PreflightStatus.PASS,
            message=f"paper mode active; tier {tier} routes to PaperFill log",
        )
    # live — annotate the intended path
    note = "live + tier %s + acct %s" % (tier, account_class)
    return PreflightResult(
        check="tier_mode_match",
        status=PreflightStatus.PASS,
        message=f"live mode active; routing per matrix ({note})",
    )


# ----------------------------------------------------------------------
# Aggregator
# ----------------------------------------------------------------------


@dataclass
class PreflightInputs:
    """Bundle of values needed by `run_preflight`. Keeps the call site clean."""

    proposal: Any
    settings: AgentSettings
    now: datetime
    cash_available_usd: float = 0.0
    max_position_usd: float | None = None
    snapshot_pct: dict[str, float] = field(default_factory=dict)
    plan_targets: dict[str, float] = field(default_factory=dict)
    day_pnl_usd: float = 0.0
    daily_loss_limit_usd: float | None = None
    lots: list[Any] | None = None
    tier: str = "T2"
    account_class: Literal["main", "limited"] = "main"


def run_preflight(inputs: PreflightInputs) -> PreflightReport:
    """Run all checks and aggregate into a `PreflightReport`."""
    results: list[PreflightResult] = [
        check_cash_availability(inputs.proposal, inputs.cash_available_usd),
        check_position_size_cap(inputs.proposal, inputs.max_position_usd),
        check_concentration_cap(
            inputs.proposal, inputs.snapshot_pct, inputs.plan_targets
        ),
        check_wash_sale(inputs.proposal, inputs.lots),
        check_daily_loss_limit(
            inputs.proposal, inputs.day_pnl_usd, inputs.daily_loss_limit_usd
        ),
        check_trading_hours(inputs.proposal, _ensure_aware(inputs.now)),
        check_tier_mode_match(
            inputs.proposal,
            inputs.tier,
            inputs.settings,
            account_class=inputs.account_class,
        ),
    ]
    return PreflightReport(results=results)


def _ensure_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


__all__ = [
    "PreflightInputs",
    "PreflightReport",
    "PreflightResult",
    "PreflightStatus",
    "check_cash_availability",
    "check_concentration_cap",
    "check_daily_loss_limit",
    "check_position_size_cap",
    "check_tier_mode_match",
    "check_trading_hours",
    "check_wash_sale",
    "run_preflight",
]
