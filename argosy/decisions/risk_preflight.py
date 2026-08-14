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
    cash_available_usd: float | None,
    *,
    estimated_cost_usd: float | None = None,
) -> PreflightResult:
    """Hard fail if a buy lacks the cash. Sells PASS by definition.

    ``cash_available_usd is None`` means the input is unavailable (caller
    omitted it AND no snapshot cash could be resolved) — that is NOT the
    same as a measured $0 balance. Treating missing as zero destroyed an
    owner-approved buy (proposal 15, 2026-07-13). Fail-loud on missing.

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

    if cash_available_usd is None:
        return PreflightResult(
            check="cash_availability",
            status=PreflightStatus.HARD_FAIL,
            message=(
                "Cash available input is unavailable (not supplied by caller "
                "and no portfolio-snapshot cash reading) — refusing to treat "
                "missing data as a measured zero balance"
            ),
            detail={"cash": None, "input_missing": True},
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


def check_sector_concentration_cap(
    proposal: Any,
    snapshot_pct: dict[str, float],
    sector_caps: dict[str, float],
    classification_map: dict[str, str],
) -> PreflightResult:
    """Hard fail if a buy would push the ticker's sector over a stated cap.

    ``sector_caps`` maps sector code → max allowed portfolio percentage (e.g.
    ``{"Tech": 35.0}``). ``classification_map`` maps ticker → sector code (e.g.
    ``{"NVDA": "Tech"}``).  Both dicts are supplied by the caller — the cap
    values come from the plan policy and the classification from the curated
    ``instrument_reference`` table; nothing is hardcoded here.

    Fail-loud contract:
    - If ``sector_caps`` is empty → PASS (no sector caps configured; skip).
    - If the proposed ticker is not in ``classification_map`` → HARD_FAIL
      ("unknown sector — cannot verify sector cap").
    - If any ticker in ``snapshot_pct`` with a non-zero weight is absent from
      ``classification_map`` → HARD_FAIL (sector total would be understated;
      cannot assert the cap holds without a complete book classification).
    - If the sector total from ``snapshot_pct`` already exceeds the cap and
      the action is a buy → HARD_FAIL.
    - Sells and holds PASS (they reduce or hold the sector weight).

    Phase 3 coarse approximation: the post-trade sector weight is not
    recomputed exactly (no pricing data at preflight time). We block on the
    *pre-trade* sector weight already breaching the cap on a buy — the same
    conservative stance as ``check_concentration_cap``.  Phase 4 can swap in
    an exact recompute.
    """
    if not sector_caps:
        return PreflightResult(
            check="sector_concentration_cap",
            status=PreflightStatus.PASS,
            message="No sector caps configured",
        )

    ticker = (getattr(proposal, "ticker", "") or "").upper()
    if not ticker:
        # Cannot evaluate => cannot pass. A tickerless proposal reaching a
        # sector cap check is a malformed input, not a clean trade.
        return PreflightResult(
            check="sector_concentration_cap",
            status=PreflightStatus.HARD_FAIL,
            message="No ticker on proposal; sector cap cannot be evaluated",
            detail={"missing_ticker": True},
        )

    # Resolve the proposed ticker's sector — unknown sector is a hard block.
    proposed_sector = classification_map.get(ticker)
    if proposed_sector is None:
        return PreflightResult(
            check="sector_concentration_cap",
            status=PreflightStatus.HARD_FAIL,
            message=(
                f"{ticker} has no sector classification; cannot verify sector cap. "
                "Add it to instrument_reference and re-seed instrument_classification."
            ),
            detail={"ticker": ticker, "missing_classification": True},
        )

    # Check sector cap exists for this ticker's sector (others are uncapped).
    cap = sector_caps.get(proposed_sector)
    if cap is None:
        return PreflightResult(
            check="sector_concentration_cap",
            status=PreflightStatus.PASS,
            message=f"{ticker} sector '{proposed_sector}' has no cap configured",
        )

    # Aggregate snapshot pct by sector; abort if any held position is unclassified
    # (an incomplete book classification means we cannot trust the sector total).
    sector_total: dict[str, float] = {}
    unclassified: list[str] = []
    for held_ticker, pct in (snapshot_pct or {}).items():
        # A None weight is missing data, not a zero weight — treating it as 0
        # would understate the sector total and let a breach through. Route it
        # to the unclassified fail-loud path.
        if pct is None:
            unclassified.append(held_ticker)
            continue
        if pct <= 0.0:
            continue
        sector = classification_map.get((held_ticker or "").upper())
        if sector is None:
            unclassified.append(held_ticker)
        else:
            sector_total[sector] = sector_total.get(sector, 0.0) + pct

    if unclassified:
        return PreflightResult(
            check="sector_concentration_cap",
            status=PreflightStatus.HARD_FAIL,
            message=(
                f"Sector total for '{proposed_sector}' cannot be computed: "
                f"{len(unclassified)} held ticker(s) lack classification: "
                f"{', '.join(sorted(unclassified)[:5])}. "
                "Add them to instrument_reference and re-seed."
            ),
            detail={"unclassified_tickers": sorted(unclassified)},
        )

    # KNOWN LIMITATION (Sol review, 2026-08-13) — this compares the CURRENT
    # sector weight to the cap, not the POST-TRADE weight. A buy that takes
    # Tech from 34% to 39% against a 35% cap is NOT caught here, because 34%
    # is still under the cap at evaluation time.
    #
    # This is the same coarse approximation the sibling single-name
    # check_concentration_cap makes ("snapshot pct + a coarse delta", see its
    # docstring) and it is deliberate rather than an oversight: PreflightInputs
    # carries no book total and no trade notional, so a true post-trade weight
    # is not computable here. Inventing an estimate would be worse than a
    # documented gap — it would read as enforcement while still missing
    # breaches. Closing this needs the size inputs threaded in, and should
    # close BOTH checks at once.
    current_sector_pct = sector_total.get(proposed_sector, 0.0)
    action = (getattr(proposal, "action", "") or "").lower()

    if current_sector_pct > cap:
        if action == "buy":
            return PreflightResult(
                check="sector_concentration_cap",
                status=PreflightStatus.HARD_FAIL,
                message=(
                    f"Sector '{proposed_sector}' is already {current_sector_pct:.1f}% "
                    f"of portfolio (cap {cap:.1f}%); buying {ticker} would push it "
                    "further over cap"
                ),
                detail={
                    "sector": proposed_sector,
                    "current_pct": current_sector_pct,
                    "cap_pct": cap,
                    "ticker": ticker,
                },
            )
        return PreflightResult(
            check="sector_concentration_cap",
            status=PreflightStatus.WARN,
            message=(
                f"Sector '{proposed_sector}' is {current_sector_pct:.1f}% "
                f"(cap {cap:.1f}%); sell reduces exposure"
            ),
        )

    return PreflightResult(
        check="sector_concentration_cap",
        status=PreflightStatus.PASS,
        message=(
            f"Sector '{proposed_sector}' at {current_sector_pct:.1f}% "
            f"is within cap {cap:.1f}%"
        ),
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
    """Bundle of values needed by `run_preflight`. Keeps the call site clean.

    ``cash_available_usd`` is ``None`` when the figure is unavailable —
    never silently coerce missing to 0.0 (proposal-15 scar).
    """

    proposal: Any
    settings: AgentSettings
    now: datetime
    cash_available_usd: float | None = None
    max_position_usd: float | None = None
    snapshot_pct: dict[str, float] = field(default_factory=dict)
    plan_targets: dict[str, float] = field(default_factory=dict)
    day_pnl_usd: float = 0.0
    daily_loss_limit_usd: float | None = None
    lots: list[Any] | None = None
    tier: str = "T2"
    account_class: Literal["main", "limited"] = "main"
    # Sector-cap fields (Phase 3 — FM-OBJ-7):
    # ``sector_caps`` maps sector_code → max portfolio % (e.g. {"Tech": 35.0}).
    # ``classification_map`` maps ticker → sector_code (e.g. {"NVDA": "Tech"}).
    # Both default to empty dict — if empty the sector cap check is skipped
    # (same behaviour as plan_targets={} for the single-name check).
    # Callers build these from the plan policy and instrument_classification;
    # the check itself is pure and does no DB access.
    sector_caps: dict[str, float] = field(default_factory=dict)
    classification_map: dict[str, str] = field(default_factory=dict)


def run_preflight(inputs: PreflightInputs) -> PreflightReport:
    """Run all checks and aggregate into a `PreflightReport`."""
    results: list[PreflightResult] = [
        check_cash_availability(inputs.proposal, inputs.cash_available_usd),
        check_position_size_cap(inputs.proposal, inputs.max_position_usd),
        check_concentration_cap(
            inputs.proposal, inputs.snapshot_pct, inputs.plan_targets
        ),
        check_sector_concentration_cap(
            inputs.proposal,
            inputs.snapshot_pct,
            inputs.sector_caps,
            inputs.classification_map,
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
    "check_sector_concentration_cap",
    "check_tier_mode_match",
    "check_trading_hours",
    "check_wash_sale",
    "run_preflight",
]
