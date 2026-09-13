"""Phase 2 — run_funnel smart refresh (codex #8): radar -> diff vs ScanState ->
estimate only new/changed -> escalate top-K go to the fleet -> persist. An
unchanged ticker (same radar fingerprint, fresh estimate) is NOT re-estimated."""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

import argosy.services.high_potential_funnel as hpf
from argosy.services.contracts import EstimatorVerdict, FleetPick
from argosy.services.trend_radar import ScanResult, TrendCandidate
from argosy.state.models import Base, Prediction, ScanState, User


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
    monkeypatch.setattr(hpf, "_load_external_candidates", lambda _uid: [])
    monkeypatch.setattr(hpf, "_load_external_quarantine", lambda _uid: [])

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
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    c = _cand("PLTR", 80.0)
    _setup(monkeypatch, [c], {}, [], fleet_pick=False)

    def estimate_via_asyncio_run(candidate, *, user_id="ariel"):
        asyncio.run(asyncio.sleep(0))  # mimics BaseAgent.run_sync's asyncio.run
        return EstimatorVerdict(ticker=candidate.ticker, go=True,
                                conviction="HIGH", sentiment=0.8, one_line="go")
    monkeypatch.setattr(hpf, "_estimate", estimate_via_asyncio_run)
    # Must NOT raise (would raise if _estimate were called inline in the loop).
    asyncio.run(hpf.run_funnel("ariel", force=False, now=now))


def test_run_funnel_offloads_blocking_radar_from_api_loop(monkeypatch):
    candidate = _cand("QURE")
    _setup(monkeypatch, [candidate], {}, [])
    started = threading.Event()
    released = threading.Event()

    def blocking_radar():
        started.set()
        # A second API coroutine must progress while this network scan waits.
        assert released.wait(timeout=5), "radar blocked the API event loop"
        return ScanResult(shortlist=(candidate,), quarantine=(), source_counts={})

    monkeypatch.setattr(hpf, "_scan_radar", blocking_radar)

    async def exercise():
        async def concurrent_request():
            assert await asyncio.to_thread(started.wait, 5)
            released.set()

        result, _ = await asyncio.gather(
            hpf.run_funnel("ariel", now=datetime.now(UTC)), concurrent_request()
        )
        return result

    result = asyncio.run(exercise())
    assert len(result.estimated) == 1


def test_radar_observation_is_persisted_before_estimator_failure(monkeypatch):
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    candidate = _cand("QURE", 91.0)
    store = _setup(monkeypatch, [candidate], {
        "OLD": {
            "ticker": "OLD",
            "last_score": 50.0,
            "radar_fingerprint": "old",
            "status": "active",
            "rank": 2,
            "quarantine_reason": "",
            "estimator_json": None,
            "fleet_json": None,
            "last_estimated_at": None,
            "last_radar_at": "2026-06-11T12:00:00+00:00",
            "last_fleet_at": None,
            "last_seen_at": "2026-06-11T12:00:00+00:00",
        },
    }, [])

    def failed_estimator(*args, **kwargs):
        raise RuntimeError("estimator unavailable")

    monkeypatch.setattr(hpf, "_estimate", failed_estimator)

    with pytest.raises(RuntimeError, match="estimator unavailable"):
        asyncio.run(hpf.run_funnel("ariel", force=False, now=now))

    assert store["QURE"]["observation"]["price"] == 100.0
    assert store["QURE"]["last_radar_at"] == now.isoformat()
    assert store["QURE"]["estimator_json"] is None
    assert store["OLD"]["status"] == "dropped"


def test_unchanged_ticker_is_not_re_estimated(monkeypatch):
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
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


def test_old_research_contract_regrades_without_reestimating(monkeypatch):
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    estimates, grades = [], []
    store = _setup(monkeypatch, [_cand('PLTR')], {}, estimates)

    async def grade(user_id, candidate, **kwargs):
        grades.append(candidate.ticker)
        return FleetPick(ticker=candidate.ticker, conviction='LOW',
                         thesis_md='Evidence remains incomplete', verdict='WATCH', cites=())

    monkeypatch.setattr(hpf, '_grade', grade)
    asyncio.run(hpf.run_funnel('ariel', now=now))
    legacy = json.loads(store['PLTR']['fleet_json'])
    legacy.pop('research_contract')
    store['PLTR']['fleet_json'] = json.dumps(legacy)
    asyncio.run(hpf.run_funnel('ariel', now=now))
    assert estimates == ['PLTR']
    assert grades == ['PLTR', 'PLTR']
    assert hpf._current_fleet_contract(store['PLTR']['fleet_json'])
    asyncio.run(hpf.run_funnel('ariel', now=now))
    assert grades == ['PLTR', 'PLTR']  # Current contract may reuse the grade.


def test_changed_fingerprint_triggers_re_estimate(monkeypatch):
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
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
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
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
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
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
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
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
    monkeypatch.setattr(hpf, "_load_external_candidates", lambda _uid: [])
    monkeypatch.setattr(hpf, "_load_external_quarantine", lambda _uid: [])

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
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
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
    assert store["PLTR"]["observation"]["price"] == 100.0
    assert store["PLTR"]["observation"]["rank"] == 1


def test_real_estimator_seam_persists_its_returned_telemetry(monkeypatch):
    from argosy.agents import quick_estimator as qe
    from argosy.services import agent_report_persistence as persistence

    verdict = EstimatorVerdict(
        ticker="IONQ",
        go=True,
        conviction="HIGH",
        sentiment=0.8,
        one_line="go",
    )
    report = SimpleNamespace(agent_role="quick_estimator")
    captured = []
    monkeypatch.setattr(
        qe,
        "estimate_with_report",
        lambda candidate, *, user_id: (verdict, report),
    )
    monkeypatch.setattr(
        persistence,
        "persist_agent_report_sync",
        lambda value, *, decision_id: captured.append((value, decision_id)),
    )

    assert hpf._estimate(_cand("IONQ"), user_id="ariel") is verdict
    assert captured == [(report, "discovery:IONQ")]


def test_persist_scan_state_writes_dated_radar_outcome_clocks(
    monkeypatch,
    tmp_path,
):
    from argosy import config

    db_path = tmp_path / "radar-clocks.db"
    url = f"sqlite:///{db_path.as_posix()}"
    engine = sa.create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="ariel"))
        db.commit()
    engine.dispose()
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(database_url=url),
    )
    observed_at = datetime(2026, 8, 29, 6, 30, tzinfo=UTC)
    state = {
        "ticker": "QURE",
        "last_score": 91.0,
        "radar_fingerprint": "q=91|f=BIOTECH",
        "status": "active",
        "rank": 1,
        "quarantine_reason": "",
        "last_radar_at": observed_at.isoformat(),
        "last_seen_at": observed_at.isoformat(),
        "observation": {
            "price": 18.25,
            "market_cap": 1_500_000_000,
            "rank": 1,
            "score": 91.0,
        },
    }

    hpf._persist_scan_states("ariel", [state])
    hpf._persist_scan_states("ariel", [state])

    engine = sa.create_engine(url)
    with Session(engine) as db:
        scan = db.get(ScanState, ("ariel", "QURE"))
        clocks = db.query(Prediction).filter_by(
            user_id="ariel",
            source="signal_stream:radar_observation",
        ).order_by(Prediction.timeframe_days).all()
        assert scan is not None
        assert [row.timeframe_days for row in clocks] == [30, 180]
        assert {row.entry_price for row in clocks} == {18.25}
        assert len({row.message_id for row in clocks}) == 2
    engine.dispose()


def test_radar_clock_failure_cannot_rollback_discovery_state(
    monkeypatch,
    tmp_path,
):
    from argosy import config
    from argosy.services.predictions import writers

    db_path = tmp_path / "radar-clock-failure.db"
    url = f"sqlite:///{db_path.as_posix()}"
    engine = sa.create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="ariel"))
        db.commit()
    engine.dispose()
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(database_url=url),
    )

    def failed_clock(*args, **kwargs):
        raise sa.exc.OperationalError("insert", {}, RuntimeError("database locked"))

    monkeypatch.setattr(writers, "write_signal_stream_predictions", failed_clock)
    observed_at = datetime(2026, 8, 29, 7, 29, tzinfo=UTC)
    estimator_json = json.dumps(
        {
            "ticker": "ONDS",
            "go": True,
            "conviction": "HIGH",
            "sentiment": 0.8,
            "one_line": "evaluate",
        }
    )
    fleet_json = json.dumps(
        {
            "ticker": "ONDS",
            "verdict": "WATCH",
            "conviction": "MED",
            "thesis_md": "Wait for contract conversion evidence.",
            "cites": [],
        }
    )
    state = {
        "ticker": "ONDS",
        "last_score": 108.2,
        "radar_fingerprint": "s=108.2|f=MOMENTUM|l=high",
        "status": "active",
        "rank": 1,
        "quarantine_reason": "",
        "estimator_json": estimator_json,
        "fleet_json": fleet_json,
        "last_estimated_at": observed_at.isoformat(),
        "last_radar_at": observed_at.isoformat(),
        "last_fleet_at": observed_at.isoformat(),
        "last_seen_at": observed_at.isoformat(),
        "observation": {"price": 12.5, "rank": 1, "score": 108.2},
    }

    hpf._persist_scan_states("ariel", [state])  # telemetry failure is swallowed

    engine = sa.create_engine(url)
    with Session(engine) as db:
        scan = db.get(ScanState, ("ariel", "ONDS"))
        assert scan is not None
        assert scan.rank == 1
        assert scan.estimator_json == estimator_json
        # The radar telemetry failed, but the actual fleet decision still has
        # a dated price and all three self-evaluation clocks.
        clocks = db.query(Prediction).filter_by(
            source="signal_stream:discovery_evaluation",
            ticker="ONDS",
        ).order_by(Prediction.timeframe_days).all()
        assert [row.timeframe_days for row in clocks] == [30, 180, 365]
        assert {row.entry_price for row in clocks} == {12.5}
    engine.dispose()
