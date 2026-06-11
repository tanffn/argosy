"""Speculative-position monitor — the exit-discipline engine for the
high-potential satellite sleeve.

The user's rule for the trending/high-risk single names: "if we buy those I
need a live daily monitor to know when to sell (or set a stop loss)." This
module is that monitor. It is deliberately MECHANICAL — high-risk names need a
pre-committed exit, not a discretionary "should I sell?" each day.

Three exit triggers, evaluated per position (see :func:`evaluate`):

  * HARD STOP — current price <= entry x (1 - hard_stop_pct). Caps the loss
    from the entry. Default 25%.
  * TRAILING STOP — current price <= peak-since-entry x (1 - trailing_pct).
    Locks in gains once a name has run, and exits a name that round-trips.
    Default 20%. The binding stop is the HIGHER of the two levels.
  * MOMENTUM BREAK — price below its 50-day moving average. A softer,
    thesis-watch signal (the trend that sourced the name has broken), surfaced
    as TRIM/WATCH rather than a hard SELL.

The engine is pure (:func:`evaluate`) so the thresholds are unit-tested
without a network. :func:`run_monitor` wraps it with a best-effort yfinance
fetch of current price, peak-since-entry, and the 50-day MA.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from argosy.logging import get_logger

log = get_logger(__name__)

Action = Literal["SELL", "TRIM", "WATCH", "HOLD"]


@dataclass(frozen=True)
class MonitorConfig:
    # hard < trailing on purpose: the hard stop is the TIGHTER absolute-loss
    # floor that binds a fresh position (peak ~= entry); the looser trailing
    # stop binds only after a name has run (peak >> entry), letting winners run
    # while still exiting on a give-back from the high. binding = max(the two
    # levels), so the higher (first-to-trigger) one always governs.
    hard_stop_pct: float = 0.20       # never lose more than 20% from entry
    trailing_stop_pct: float = 0.25   # exit if a runner gives back 25% from peak
    watch_band_pct: float = 0.05      # WATCH when within 5% of the binding stop
    use_ma_break: bool = True         # surface a momentum break below the 50d MA


@dataclass(frozen=True)
class PositionSnapshot:
    ticker: str
    entry_price: float
    current_price: float
    peak_price: float            # highest close since entry (>= entry_price)
    ma_50: float | None = None
    name: str = ""


@dataclass(frozen=True)
class MonitorSignal:
    ticker: str
    name: str
    action: Action
    reason: str
    current_price: float
    entry_price: float
    peak_price: float
    hard_stop_level: float
    trailing_stop_level: float
    binding_stop_level: float    # max(hard, trailing) — the one that triggers first
    pct_from_entry: float        # +gain / -loss vs entry, in %
    pct_from_peak: float         # drawdown from peak, in % (<= 0)
    distance_to_stop_pct: float  # how far current is above the binding stop, in %


def evaluate(pos: PositionSnapshot, cfg: MonitorConfig | None = None) -> MonitorSignal:
    """Evaluate one position against the stop rules. Pure; no I/O."""
    c = cfg or MonitorConfig()
    entry = pos.entry_price
    peak = max(pos.peak_price, entry)  # peak can never be below entry
    cur = pos.current_price

    hard_level = entry * (1.0 - c.hard_stop_pct)
    trail_level = peak * (1.0 - c.trailing_stop_pct)
    binding = max(hard_level, trail_level)

    pct_entry = ((cur - entry) / entry * 100.0) if entry else 0.0
    pct_peak = ((cur - peak) / peak * 100.0) if peak else 0.0
    dist_to_stop = ((cur - binding) / binding * 100.0) if binding else 0.0

    if cur <= hard_level and cur <= trail_level:
        action: Action = "SELL"
        reason = (
            f"BOTH stops breached: price ${cur:.2f} <= hard ${hard_level:.2f} "
            f"(-{c.hard_stop_pct:.0%} from entry) and trailing ${trail_level:.2f} "
            f"(-{c.trailing_stop_pct:.0%} from peak ${peak:.2f}). Exit."
        )
    elif cur <= hard_level:
        action = "SELL"
        reason = (
            f"HARD STOP: price ${cur:.2f} <= ${hard_level:.2f} "
            f"(-{c.hard_stop_pct:.0%} from entry ${entry:.2f}). Cut the loss."
        )
    elif cur <= trail_level:
        action = "SELL"
        reason = (
            f"TRAILING STOP: price ${cur:.2f} <= ${trail_level:.2f} "
            f"(-{c.trailing_stop_pct:.0%} from peak ${peak:.2f}). Lock in / exit."
        )
    elif dist_to_stop <= c.watch_band_pct * 100.0:
        action = "WATCH"
        reason = (
            f"Within {dist_to_stop:.1f}% of the binding stop ${binding:.2f} — "
            "tighten attention; a stop trigger is near."
        )
    elif c.use_ma_break and pos.ma_50 is not None and cur < pos.ma_50:
        action = "TRIM"
        reason = (
            f"MOMENTUM BREAK: price ${cur:.2f} below the 50-day MA "
            f"${pos.ma_50:.2f} — the sourcing trend has weakened. Consider "
            "trimming; the hard/trailing stops remain the hard exit."
        )
    else:
        action = "HOLD"
        reason = (
            f"Above stops: ${cur:.2f} vs binding stop ${binding:.2f} "
            f"({dist_to_stop:+.1f}% cushion). Thesis intact."
        )

    return MonitorSignal(
        ticker=pos.ticker,
        name=pos.name or pos.ticker,
        action=action,
        reason=reason,
        current_price=round(cur, 2),
        entry_price=round(entry, 2),
        peak_price=round(peak, 2),
        hard_stop_level=round(hard_level, 2),
        trailing_stop_level=round(trail_level, 2),
        binding_stop_level=round(binding, 2),
        pct_from_entry=round(pct_entry, 1),
        pct_from_peak=round(pct_peak, 1),
        distance_to_stop_pct=round(dist_to_stop, 1),
    )


def _fetch_history_stats(ticker: str, since: date) -> dict[str, float] | None:
    """Best-effort yfinance fetch: current close, peak close since ``since``,
    and the 50-day moving average. Returns None on any failure."""
    try:
        import yfinance as yf

        hist = yf.Ticker(ticker).history(period="1y", auto_adjust=True)
        if hist is None or hist.empty:
            return None
        closes = hist["Close"].dropna()
        if closes.empty:
            return None
        current = float(closes.iloc[-1])
        # peak + entry-reference since the entry date (tz-naive date compare)
        since_ts = str(since)
        recent = closes[closes.index >= since_ts]
        peak = float(recent.max()) if not recent.empty else current
        entry_close = float(recent.iloc[0]) if not recent.empty else current
        ma_50 = float(closes.tail(50).mean()) if len(closes) >= 50 else None
        out = {"current": current, "peak": peak, "entry_close": entry_close}
        if ma_50 is not None:
            out["ma_50"] = ma_50
        return out
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.warning("speculative_monitor.fetch_failed", ticker=ticker, error=str(exc)[:120])
        return None


@dataclass(frozen=True)
class WatchEntry:
    ticker: str
    entry_price: float
    entry_date: date
    name: str = ""


def run_monitor(
    watch: list[WatchEntry],
    *,
    cfg: MonitorConfig | None = None,
) -> list[MonitorSignal]:
    """Fetch live stats for each watched position and evaluate the stops.

    A position whose stats can't be fetched is skipped (logged), never
    fabricated. Returns signals sorted with the most actionable first
    (SELL > TRIM > WATCH > HOLD).
    """
    order = {"SELL": 0, "TRIM": 1, "WATCH": 2, "HOLD": 3}
    signals: list[MonitorSignal] = []
    for w in watch:
        stats = _fetch_history_stats(w.ticker, w.entry_date)
        if stats is None:
            continue
        # explicit cost basis wins; else anchor to the close at the entry date
        entry = w.entry_price or stats.get("entry_close") or stats["current"]
        signals.append(evaluate(
            PositionSnapshot(
                ticker=w.ticker,
                entry_price=entry,
                current_price=stats["current"],
                peak_price=stats.get("peak", stats["current"]),
                ma_50=stats.get("ma_50"),
                name=w.name,
            ),
            cfg,
        ))
    signals.sort(key=lambda s: (order.get(s.action, 9), s.distance_to_stop_pct))
    return signals


__all__ = [
    "MonitorConfig",
    "PositionSnapshot",
    "MonitorSignal",
    "WatchEntry",
    "evaluate",
    "run_monitor",
]
