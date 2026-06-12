"""Phase 2 — run_funnel smart refresh (codex #8): radar -> diff vs ScanState ->
estimate only new/changed -> escalate top-K go to the fleet -> persist. An
unchanged ticker (same radar fingerprint, fresh estimate) is NOT re-estimated."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import argosy.services.high_potential_funnel as hpf
from argosy.services.contracts import EstimatorVerdict, FleetPick
from argosy.services.trend_radar import ScanResult, TrendCandidate


def _cand(ticker, score=80.0):
    return TrendCandidate(ticker=ticker, name=ticker, score=score,
                          families=("MOMENTUM",), reasons=("trend",),
                          price=100.0, market_cap=5e9, dollar_volume=2e8,
                          pct_change=4.0)


def _setup(monkeypatch, shortlist, existing, estimate_calls, *, fleet_pick=True):
    monkeypatch.setattr(hpf, "_scan_radar",
                        lambda: ScanResult(shortlist=tuple(shortlist),
                                           quarantine=(), source_counts={}))

    def fake_estimate(candidate, *, user_id="ariel"):
        estimate_calls.append(candidate.ticker)
        return EstimatorVerdict(ticker=candidate.ticker, go=True,
                                conviction="HIGH", sentiment=0.8, one_line="go")
    monkeypatch.setattr(hpf, "_estimate", fake_estimate)

    async def fake_grade(user_id, candidate, **kwargs):
        if not fleet_pick:
            return None
        return FleetPick(ticker=candidate.ticker, conviction="HIGH",
                         thesis_md="t", verdict="BUY", cites=())
    monkeypatch.setattr(hpf, "_grade", fake_grade)

    store = dict(existing)
    monkeypatch.setattr(hpf, "_load_scan_states", lambda uid: dict(store))

    def fake_persist(uid, states):
        store.clear()
        for s in states:
            store[s["ticker"]] = s
    monkeypatch.setattr(hpf, "_persist_scan_states", fake_persist)
    return store


def test_run_funnel_offloads_sync_estimator_off_event_loop(monkeypatch):
    """Live regression: the funnel is async, but the real estimator uses
    run_sync (asyncio.run internally). Calling it directly in the running loop
    raises 'asyncio.run() cannot be called from a running event loop'. The funnel
    must offload it to a thread."""
    now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
    c = _cand("PLTR", 80.0)
    _setup(monkeypatch, [c], {}, [], fleet_pick=False)

    def estimate_via_asyncio_run(candidate, *, user_id="ariel"):
        asyncio.run(asyncio.sleep(0))  # mimics BaseAgent.run_sync's asyncio.run
        return EstimatorVerdict(ticker=candidate.ticker, go=True,
                                conviction="HIGH", sentiment=0.8, one_line="go")
    monkeypatch.setattr(hpf, "_estimate", estimate_via_asyncio_run)
    # Must NOT raise (would raise if _estimate were called inline in the loop).
    asyncio.run(hpf.run_funnel("ariel", force=False, now=now))


def test_unchanged_ticker_is_not_re_estimated(monkeypatch):
    now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
    c = _cand("PLTR", 80.0)
    fp = hpf.radar_fingerprint(c)
    existing = {"PLTR": {
        "ticker": "PLTR", "last_score": 80.0, "radar_fingerprint": fp,
        "status": "active", "rank": 1, "quarantine_reason": "",
        "estimator_json": json.dumps({"ticker": "PLTR", "go": True,
            "conviction": "HIGH", "sentiment": 0.8, "one_line": "cached"}),
        "fleet_json": None,
        "last_estimated_at": "2026-06-12T11:00:00+00:00",  # 1h ago, fresh
        "last_radar_at": "2026-06-12T11:00:00+00:00",
        "last_fleet_at": None, "last_seen_at": "2026-06-12T11:00:00+00:00",
    }}
    calls: list[str] = []
    _setup(monkeypatch, [c], existing, calls)
    result = asyncio.run(hpf.run_funnel("ariel", force=False, now=now))
    assert calls == []  # PLTR reused, NOT re-estimated
    assert any(v.ticker == "PLTR" for v in result.estimated)


def test_changed_fingerprint_triggers_re_estimate(monkeypatch):
    now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
    c = _cand("PLTR", 95.0)  # score moved -> fingerprint changes
    existing = {"PLTR": {
        "ticker": "PLTR", "last_score": 80.0,
        "radar_fingerprint": "s=80.0|f=MOMENTUM|l=high", "status": "active",
        "rank": 1, "quarantine_reason": "",
        "estimator_json": json.dumps({"ticker": "PLTR", "go": True,
            "conviction": "HIGH", "sentiment": 0.8, "one_line": "old"}),
        "fleet_json": None, "last_estimated_at": "2026-06-12T11:00:00+00:00",
        "last_radar_at": "2026-06-12T11:00:00+00:00", "last_fleet_at": None,
        "last_seen_at": "2026-06-12T11:00:00+00:00",
    }}
    calls: list[str] = []
    _setup(monkeypatch, [c], existing, calls)
    asyncio.run(hpf.run_funnel("ariel", force=False, now=now))
    assert calls == ["PLTR"]  # re-estimated because the fingerprint moved


def test_changed_fp_does_not_carry_stale_fleet_json(monkeypatch):
    """codex p2 #1/#2: when the fingerprint moves and the new grade is None, the
    OLD fleet_json must NOT be persisted under the new fingerprint."""
    now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
    c = _cand("PLTR", 95.0)  # score moved -> new fingerprint
    existing = {"PLTR": {
        "ticker": "PLTR", "last_score": 80.0,
        "radar_fingerprint": "s=80.0|f=MOMENTUM|l=high", "status": "active",
        "rank": 1, "quarantine_reason": "",
        "estimator_json": json.dumps({"ticker": "PLTR", "go": True,
            "conviction": "HIGH", "sentiment": 0.8, "one_line": "old"}),
        "fleet_json": json.dumps({"ticker": "PLTR", "conviction": "HIGH",
            "verdict": "BUY", "thesis_md": "old", "cites": []}),
        "last_estimated_at": now.isoformat(), "last_radar_at": now.isoformat(),
        "last_fleet_at": now.isoformat(), "last_seen_at": now.isoformat()}}
    calls: list[str] = []
    store = _setup(monkeypatch, [c], existing, calls, fleet_pick=False)  # grade -> None
    asyncio.run(hpf.run_funnel("ariel", force=False, now=now))
    assert store["PLTR"]["fleet_json"] is None  # stale BUY not carried forward


def test_naive_stored_timestamp_does_not_crash_reuse(monkeypatch):
    """codex p2 #5: SQLite returns naive datetimes; reuse must not crash when
    diffed against an aware `now`."""
    now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
    c = _cand("PLTR", 80.0)
    fp = hpf.radar_fingerprint(c)
    existing = {"PLTR": {
        "ticker": "PLTR", "last_score": 80.0, "radar_fingerprint": fp,
        "status": "active", "rank": 1, "quarantine_reason": "",
        "estimator_json": json.dumps({"ticker": "PLTR", "go": True,
            "conviction": "HIGH", "sentiment": 0.8, "one_line": "c"}),
        "fleet_json": None,
        "last_estimated_at": "2026-06-12T11:00:00",  # NAIVE (no tz), 1h ago
        "last_radar_at": "2026-06-12T11:00:00", "last_fleet_at": None,
        "last_seen_at": "2026-06-12T11:00:00"}}
    calls: list[str] = []
    _setup(monkeypatch, [c], existing, calls)
    asyncio.run(hpf.run_funnel("ariel", force=False, now=now))  # must not raise
    assert calls == []  # naive-but-fresh timestamp -> reused


def test_quarantined_ticker_not_marked_dropped(monkeypatch):
    """codex p2 #6: a seen-but-quarantined ticker is 'quarantined', not 'dropped'."""
    now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
    existing = {"PLTR": {
        "ticker": "PLTR", "last_score": 80.0, "radar_fingerprint": "x",
        "status": "active", "rank": 1, "quarantine_reason": "",
        "estimator_json": None, "fleet_json": None, "last_estimated_at": None,
        "last_radar_at": now.isoformat(), "last_fleet_at": None,
        "last_seen_at": now.isoformat()}}
    monkeypatch.setattr(hpf, "_scan_radar",
                        lambda: ScanResult(shortlist=(),
                                           quarantine=(("PLTR", "failed-liquidity"),),
                                           source_counts={}))
    monkeypatch.setattr(hpf, "_estimate", lambda c, **k: None)

    async def fake_grade(u, c, **k):
        return None
    monkeypatch.setattr(hpf, "_grade", fake_grade)
    store = dict(existing)
    monkeypatch.setattr(hpf, "_load_scan_states", lambda uid: dict(store))

    def persist(uid, states):
        store.clear()
        for s in states:
            store[s["ticker"]] = s
    monkeypatch.setattr(hpf, "_persist_scan_states", persist)
    asyncio.run(hpf.run_funnel("ariel", force=False, now=now))
    assert store["PLTR"]["status"] == "quarantined"
    assert store["PLTR"]["quarantine_reason"] == "failed-liquidity"


def test_funnel_escalates_go_names_and_drops_absent(monkeypatch):
    now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
    new = _cand("PLTR", 90.0)
    existing = {"OLDX": {  # not in the new radar -> should be marked dropped
        "ticker": "OLDX", "last_score": 50.0, "radar_fingerprint": "x",
        "status": "active", "rank": 9, "quarantine_reason": "",
        "estimator_json": None, "fleet_json": None,
        "last_estimated_at": None, "last_radar_at": "2026-06-10T00:00:00+00:00",
        "last_fleet_at": None, "last_seen_at": "2026-06-10T00:00:00+00:00"}}
    calls: list[str] = []
    store = _setup(monkeypatch, [new], existing, calls)
    result = asyncio.run(hpf.run_funnel("ariel", force=False, now=now))
    assert [p.ticker for p in result.picks] == ["PLTR"]
    assert store["OLDX"]["status"] == "dropped"
    assert store["PLTR"]["status"] == "active"
