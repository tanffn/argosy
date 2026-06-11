"""Tests for the speculative-position stop-loss / sell-signal engine (pure)."""
from __future__ import annotations

from argosy.services.speculative_monitor import (
    MonitorConfig,
    PositionSnapshot,
    evaluate,
)


def _pos(**kw) -> PositionSnapshot:
    base = dict(ticker="SPEC", entry_price=100.0, current_price=100.0,
                peak_price=100.0)
    base.update(kw)
    return PositionSnapshot(**base)


def test_hold_when_above_stops():
    sig = evaluate(_pos(current_price=110.0, peak_price=112.0))
    assert sig.action == "HOLD"
    assert sig.pct_from_entry == 10.0


def test_hard_stop_triggers_sell():
    # entry 100, -20% hard stop = 80 (the binding floor on a fresh position).
    # price 79 -> hard-only SELL (trailing level is 75, not yet breached).
    sig = evaluate(_pos(current_price=79.0, peak_price=100.0))
    assert sig.action == "SELL"
    assert "HARD STOP" in sig.reason
    assert sig.hard_stop_level == 80.0


def test_trailing_stop_triggers_sell_after_run():
    # ran to 200, -25% trailing = 150. price 149 -> SELL even though +49% vs entry.
    sig = evaluate(_pos(current_price=149.0, peak_price=200.0))
    assert sig.action == "SELL"
    assert "TRAILING STOP" in sig.reason
    assert sig.trailing_stop_level == 150.0
    assert sig.pct_from_entry == 49.0  # still in profit, but discipline exits


def test_binding_stop_is_the_higher_level():
    # fresh position: hard 80 > trailing (peak==entry -> 75). binding = 80.
    assert evaluate(_pos(current_price=110.0, peak_price=100.0)).binding_stop_level == 80.0
    # runner: peak 200 -> trailing 150 > hard 80. binding = 150.
    assert evaluate(_pos(current_price=170.0, peak_price=200.0)).binding_stop_level == 150.0


def test_watch_band_near_stop():
    # peak 200 -> trailing 150 (binding). price 153 is +2% above 150 -> WATCH.
    sig = evaluate(_pos(current_price=153.0, peak_price=200.0))
    assert sig.action == "WATCH"


def test_ma_break_trims_when_above_stops():
    # comfortably above stops but below the 50d MA -> momentum-break TRIM.
    sig = evaluate(_pos(current_price=110.0, peak_price=112.0, ma_50=120.0))
    assert sig.action == "TRIM"
    assert "MOMENTUM BREAK" in sig.reason


def test_stop_dominates_ma_break():
    # below hard stop AND below MA -> SELL wins (hard exit dominates).
    sig = evaluate(_pos(current_price=70.0, peak_price=100.0, ma_50=90.0))
    assert sig.action == "SELL"


def test_peak_never_below_entry():
    # a freshly-added name with peak == entry and a small dip is HOLD, not a
    # spurious trailing trigger from peak < entry.
    sig = evaluate(_pos(current_price=98.0, peak_price=100.0))
    assert sig.peak_price == 100.0
    assert sig.action == "HOLD"


def test_custom_tighter_stops():
    cfg = MonitorConfig(hard_stop_pct=0.10, trailing_stop_pct=0.08)
    # entry 100 -> hard 90. price 89 -> SELL at the tighter 10%.
    sig = evaluate(_pos(current_price=89.0, peak_price=100.0), cfg)
    assert sig.action == "SELL"
    assert sig.hard_stop_level == 90.0


def test_drawdown_from_peak_reported():
    sig = evaluate(_pos(current_price=150.0, peak_price=200.0))
    assert sig.pct_from_peak == -25.0
