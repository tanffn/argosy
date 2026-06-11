"""Tests for the daily speculative-monitor cadence loop (no network)."""
from __future__ import annotations

import asyncio

import argosy.services.speculative_monitor as monitor_mod
from argosy.orchestrator.loops.speculative_monitor_loop import SpeculativeMonitorLoop
from argosy.services.speculative_monitor import MonitorSignal


def _signal(ticker: str, action: str) -> MonitorSignal:
    return MonitorSignal(
        ticker=ticker, name=ticker, action=action, reason="test",
        current_price=10.0, entry_price=12.0, peak_price=15.0,
        hard_stop_level=9.6, trailing_stop_level=11.25, binding_stop_level=11.25,
        pct_from_entry=-16.7, pct_from_peak=-33.3, distance_to_stop_pct=-11.0,
    )


def test_tick_reports_actionable_counts(monkeypatch):
    # Stub the monitor so the loop never hits the network.
    def fake_run_monitor(watch, *, cfg=None):
        return [_signal("AMD", "HOLD"), _signal("SOFI", "SELL"), _signal("TSLA", "TRIM")]

    monkeypatch.setattr(monitor_mod, "run_monitor", fake_run_monitor)

    published: list[tuple[str, dict]] = []

    async def fake_publish(topic, payload):
        published.append((topic, payload))

    import argosy.orchestrator.loops.speculative_monitor_loop as loop_mod

    monkeypatch.setattr(loop_mod, "publish_event", fake_publish)
    monkeypatch.setattr(loop_mod, "_held_speculative_tickers", lambda: ["AMD", "SOFI", "TSLA"])

    loop = SpeculativeMonitorLoop()
    summary = asyncio.run(loop.tick())

    assert summary == {"watched": 3, "actionable": 2, "sell": 1}
    # SELL + TRIM published; HOLD not.
    topics = {p["ticker"]: p["action"] for _, p in published}
    assert topics == {"SOFI": "SELL", "TSLA": "TRIM"}


def test_tick_no_held_names_is_noop(monkeypatch):
    import argosy.orchestrator.loops.speculative_monitor_loop as loop_mod

    monkeypatch.setattr(loop_mod, "_held_speculative_tickers", lambda: [])
    summary = asyncio.run(SpeculativeMonitorLoop().tick())
    assert summary["watched"] == 0 and summary["actionable"] == 0


def test_loop_registered_in_scheduler():
    from argosy.orchestrator.scheduler import Scheduler

    sched = Scheduler(user_id="ariel")
    sched.register_default_loops()
    assert "speculative_monitor" in sched._loops
