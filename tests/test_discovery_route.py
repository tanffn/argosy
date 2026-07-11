"""Phase 2 — combined discovery endpoint (codex #12: NEW DTO, old sleeve kept)
+ the separate DiscoveryFunnelLoop (codex #10)."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import argosy.api.routes.portfolio as portfolio_routes
from argosy.api.main import create_app
from argosy.services.contracts import EstimatorVerdict, FleetPick
from argosy.services.high_potential_funnel import (
    FunnelResult,
    _pick_to_json,
    _verdict_to_json,
)
from argosy.state.models import (
    Base,
    EvaluationMethod,
    Prediction,
    PredictionOutcome,
    Proposal,
    ScanState,
)


def _empty_transparency():
    return [], {
        "tracked": 0,
        "active": 0,
        "quarantined": 0,
        "dropped_stale": 0,
        "estimated": 0,
        "estimator_go": 0,
        "fleet_graded": 0,
        "fleet_buy": 0,
        "open_trade_proposals": 0,
    }, []


def _discovery_db(tmp_path, monkeypatch):
    db_path = tmp_path / "discovery.db"
    url = f"sqlite:///{db_path}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        portfolio_routes,
        "get_settings",
        lambda: SimpleNamespace(database_url=url),
    )
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _set_signal_streams_enabled(monkeypatch, enabled: bool) -> None:
    monkeypatch.setattr(
        "argosy.config.load_signal_streams_config",
        lambda user_id: SimpleNamespace(enabled=enabled),
    )


def test_discovery_transparency_lists_only_enabled_signal_streams_with_beta_zero(
    tmp_path,
    monkeypatch,
) -> None:
    from argosy.config import (
        GovContractsSignalConfig,
        InsiderClusterSignalConfig,
        SignalStreamsConfig,
    )

    engine, _factory = _discovery_db(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "argosy.config.load_signal_streams_config",
        lambda user_id: SignalStreamsConfig(
            gov_contracts=GovContractsSignalConfig(enabled=False),
            insider_cluster=InsiderClusterSignalConfig(enabled=True),
        ),
    )

    response = TestClient(create_app()).get(
        "/api/portfolio/discovery",
        params={"user_id": "ariel"},
    )

    assert response.status_code == 200
    sources = response.json()["sources"]
    assert "gov_contracts" not in {source["key"] for source in sources}
    insider = next(
        source for source in sources if source["key"] == "insider_cluster"
    )
    assert insider["label"] == "Insider clusters"
    assert insider["scorecard"]["source"] == "signal_stream:insider_cluster"
    assert insider["scorecard"]["scored_outcomes"] == 0
    assert insider["scorecard"]["calibration"] == (
        "uncalibrated (beta — 0 scored over 0 days)"
    )
    engine.dispose()


def test_get_discovery_reads_cached_state(monkeypatch):
    picks = [FleetPick(ticker="PLTR", conviction="HIGH", thesis_md="AI platform",
                       verdict="BUY", cites=("fundamentals",))]
    est = [EstimatorVerdict(ticker="PLTR", go=True, conviction="HIGH",
                            sentiment=0.8, one_line="go")]
    monkeypatch.setattr(portfolio_routes, "_load_discovery_state",
                        lambda user_id: (picks, est, "2026-06-12T12:00:00+00:00"))
    monkeypatch.setattr(
        portfolio_routes, "_load_discovery_transparency",
        lambda user_id: _empty_transparency(),
        raising=False,
    )
    client = TestClient(create_app())
    r = client.get("/api/portfolio/discovery", params={"user_id": "ariel"})
    assert r.status_code == 200
    body = r.json()
    assert body["picks"][0]["ticker"] == "PLTR"
    assert body["picks"][0]["verdict"] == "BUY"
    assert body["last_refreshed_at"] == "2026-06-12T12:00:00+00:00"
    # conviction-only: no dollar amounts on the discovery surface
    assert "amount_usd" not in body["picks"][0]


def test_post_refresh_runs_funnel(monkeypatch):
    import argosy.services.high_potential_funnel as hpf

    async def fake_run(user_id, *, force=False, now=None):
        return FunnelResult(
            picks=[FleetPick(ticker="NVDA", conviction="HIGH", thesis_md="x",
                             verdict="BUY", cites=())],
            estimated=[EstimatorVerdict(ticker="NVDA", go=True, conviction="HIGH",
                                        sentiment=0.9, one_line="go")],
            radar=[], last_refreshed_at="2026-06-12T13:00:00+00:00")
    monkeypatch.setattr(hpf, "run_funnel", fake_run)
    monkeypatch.setattr(
        portfolio_routes, "_load_discovery_transparency",
        lambda user_id: _empty_transparency(),
        raising=False,
    )
    client = TestClient(create_app())
    r = client.post("/api/portfolio/discovery/refresh",
                    params={"user_id": "ariel", "force": "true"})
    assert r.status_code == 200
    body = r.json()
    assert [p["ticker"] for p in body["picks"]] == ["NVDA"]
    assert body["last_refreshed_at"] == "2026-06-12T13:00:00+00:00"


def test_discovery_reports_persisted_sources_stages_and_ticker_provenance(
    tmp_path,
    monkeypatch,
):
    engine, factory = _discovery_db(tmp_path, monkeypatch)
    observed = datetime(2026, 7, 11, 5, 0, tzinfo=UTC)
    estimator = EstimatorVerdict(
        ticker="CMPS",
        go=True,
        conviction="MED",
        sentiment=0.72,
        one_line="promising but still early",
    )
    fleet = FleetPick(
        ticker="CMPS",
        conviction="HIGH",
        thesis_md="Asymmetric research setup",
        verdict="BUY",
        cites=("10-K",),
    )
    with factory() as db:
        db.add_all([
            ScanState(
                user_id="ariel",
                ticker="CMPS",
                status="active",
                rank=1,
                last_score=82.5,
                radar_fingerprint="s=82.5|f=GROWTH,MOMENTUM|l=high",
                nomination_evidence_json=json.dumps({
                    "stream": "gov_contracts",
                    "dedup_key": "usaspending:CMPS-1",
                    "evidence": {"award_url": "https://example.test/CMPS-1"},
                }),
                estimator_json=_verdict_to_json(estimator),
                fleet_json=_pick_to_json(fleet),
                last_radar_at=observed,
            ),
            ScanState(
                user_id="ariel",
                ticker="NEXT",
                status="active",
                rank=2,
                last_score=65.0,
                radar_fingerprint="s=65.0|f=ATTENTION,MOMENTUM|l=mid",
                estimator_json=_verdict_to_json(EstimatorVerdict(
                    ticker="NEXT",
                    go=False,
                    conviction="LOW",
                    sentiment=-0.1,
                    one_line="not enough evidence",
                )),
                last_radar_at=observed,
            ),
            ScanState(
                user_id="ariel",
                ticker="ILLIQ",
                status="quarantined",
                rank=3,
                last_score=30.0,
                radar_fingerprint="s=30.0|f=ATTENTION|l=low",
                quarantine_reason="failed-liquidity",
                last_radar_at=observed,
            ),
            ScanState(
                user_id="ariel",
                ticker="OLD",
                status="dropped",
                rank=4,
                last_score=20.0,
                radar_fingerprint="s=20.0|f=GROWTH|l=low",
                last_radar_at=observed,
            ),
            Proposal(
                user_id="ariel",
                ticker="CMPS",
                action="buy",
                tier="T2",
                status="rejected",
                confidence="HIGH",
                decision_run_id=40,
                created_at=datetime(2026, 7, 10, 8, tzinfo=UTC),
            ),
            Proposal(
                user_id="ariel",
                ticker="CMPS",
                action="buy",
                tier="T2",
                status="awaiting_human",
                confidence="MEDIUM",
                decision_run_id=41,
                created_at=datetime(2026, 7, 11, 6, tzinfo=UTC),
            ),
        ])
        db.commit()

    client = TestClient(create_app())
    response = client.get("/api/portfolio/discovery", params={"user_id": "ariel"})
    assert response.status_code == 200
    body = response.json()

    assert body["stages"] == {
        "tracked": 4,
        "active": 2,
        "quarantined": 1,
        "dropped_stale": 1,
        "estimated": 2,
        "estimator_go": 1,
        "fleet_graded": 1,
        "fleet_buy": 1,
        "open_trade_proposals": 1,
    }
    assert [
        {key: value for key, value in source.items() if key != "scorecard"}
        for source in body["sources"]
    ] == [
        {
            "key": "attention",
            "label": "Attention",
            "tracked_count": 2,
            "active_count": 1,
            "quarantined_count": 1,
            "dropped_stale_count": 0,
        },
        {
            "key": "gov_contracts",
            "label": "Government contracts",
            "tracked_count": 1,
            "active_count": 1,
            "quarantined_count": 0,
            "dropped_stale_count": 0,
        },
        {
            "key": "growth",
            "label": "Growth fundamentals",
            "tracked_count": 2,
            "active_count": 1,
            "quarantined_count": 0,
            "dropped_stale_count": 1,
        },
        {
            "key": "momentum",
            "label": "Momentum",
            "tracked_count": 2,
            "active_count": 2,
            "quarantined_count": 0,
            "dropped_stale_count": 0,
        },
    ]
    assert all(
        source["scorecard"] is None
        for source in body["sources"]
        if source["key"] != "gov_contracts"
    )
    government_scorecard = next(
        source["scorecard"]
        for source in body["sources"]
        if source["key"] == "gov_contracts"
    )
    assert government_scorecard["scored_outcomes"] == 0
    assert "insider_cluster" not in {source["key"] for source in body["sources"]}

    cmps = next(row for row in body["candidates"] if row["ticker"] == "CMPS")
    assert cmps["source_keys"] == ["gov_contracts", "growth", "momentum"]
    assert cmps["source_labels"] == [
        "Government contracts",
        "Growth fundamentals",
        "Momentum",
    ]
    assert cmps["estimator"]["conviction"] == "MED"
    assert cmps["fleet"]["conviction"] == "HIGH"
    assert cmps["fleet"]["verdict"] == "BUY"
    assert cmps["latest_trade_proposal"] == {
        "id": 2,
        "action": "buy",
        "confidence": "MEDIUM",
        "status": "awaiting_human",
        "decision_run_id": 41,
        "created_at": "2026-07-11T06:00:00",
    }
    engine.dispose()


def test_discovery_lists_enabled_sources_even_with_zero_tracked_tickers(
    tmp_path,
    monkeypatch,
):
    engine, _factory = _discovery_db(tmp_path, monkeypatch)
    _set_signal_streams_enabled(monkeypatch, True)

    response = TestClient(create_app()).get(
        "/api/portfolio/discovery",
        params={"user_id": "ariel"},
    )
    assert response.status_code == 200
    sources = response.json()["sources"]
    assert [
        {key: value for key, value in source.items() if key != "scorecard"}
        for source in sources
    ] == [
        {
            "key": "attention",
            "label": "Attention",
            "tracked_count": 0,
            "active_count": 0,
            "quarantined_count": 0,
            "dropped_stale_count": 0,
        },
        {
            "key": "gov_contracts",
            "label": "Government contracts",
            "tracked_count": 0,
            "active_count": 0,
            "quarantined_count": 0,
            "dropped_stale_count": 0,
        },
        {
            "key": "growth",
            "label": "Growth fundamentals",
            "tracked_count": 0,
            "active_count": 0,
            "quarantined_count": 0,
            "dropped_stale_count": 0,
        },
        {
            "key": "momentum",
            "label": "Momentum",
            "tracked_count": 0,
            "active_count": 0,
            "quarantined_count": 0,
            "dropped_stale_count": 0,
        },
    ]
    assert next(
        source for source in sources if source["key"] == "gov_contracts"
    )["scorecard"]["calibration"] == (
        "uncalibrated (beta — 0 scored over 0 days)"
    )
    assert all(
        source["scorecard"] is None
        for source in sources
        if source["key"] != "gov_contracts"
    )
    engine.dispose()


def test_discovery_enabled_signal_source_exposes_zero_scored_beta(
    tmp_path,
    monkeypatch,
):
    engine, factory = _discovery_db(tmp_path, monkeypatch)
    _set_signal_streams_enabled(monkeypatch, True)
    event_at = datetime.now(UTC)
    with factory() as db:
        for index in range(16):
            db.add(
                Prediction(
                    user_id="ariel",
                    source="signal_stream:gov_contracts",
                    source_ref=json.dumps({"award": index}),
                    ticker=f"T{index}",
                    direction="long",
                    entry_price=Decimal("25"),
                    timeframe_days=30,
                    message_id=f"zero-outcome:{index}",
                    event_at=event_at,
                    evaluation_due_at=event_at,
                    evaluation_method="fixed_lookahead_30d",
                )
            )
        db.commit()

    response = TestClient(create_app()).get(
        "/api/portfolio/discovery",
        params={"user_id": "ariel"},
    )

    assert response.status_code == 200
    sources = response.json()["sources"]
    government = next(source for source in sources if source["key"] == "gov_contracts")
    assert government["scorecard"]["scored_outcomes"] == 0
    assert government["scorecard"]["horizons"]["30d"]["scored_outcomes"] == 0
    assert government["scorecard"]["horizons"]["180d"]["scored_outcomes"] == 0
    assert government["scorecard"]["calibration"] == (
        "uncalibrated (beta — 0 scored over 0 days)"
    )
    assert government["scorecard"]["funnel_context_enabled"] is True
    assert all(
        source["scorecard"] is None
        for source in sources
        if source["key"] != "gov_contracts"
    )
    engine.dispose()


def test_discovery_exposes_killed_signal_scorecard(
    tmp_path,
    monkeypatch,
):
    engine, factory = _discovery_db(tmp_path, monkeypatch)
    _set_signal_streams_enabled(monkeypatch, True)
    event_at = datetime(2025, 12, 1, tzinfo=UTC)
    with factory() as db:
        db.add(
            EvaluationMethod(
                method_name="fixed_lookahead_180d",
                family="fixed_lookahead",
                method_version=1,
                is_active=1,
            )
        )
        db.flush()
        for index in range(50):
            prediction = Prediction(
                user_id="ariel",
                source="signal_stream:gov_contracts",
                source_ref=json.dumps({"award": index}),
                ticker=f"T{index}",
                direction="long",
                entry_price=Decimal("100"),
                timeframe_days=180,
                message_id=f"killed:{index}",
                event_at=event_at,
                evaluation_due_at=event_at,
                evaluation_method="fixed_lookahead_180d",
            )
            db.add(prediction)
            db.flush()
            db.add(
                PredictionOutcome(
                    prediction_id=prediction.id,
                    evaluation_method="fixed_lookahead_180d",
                    outcome_kind="hit_stop",
                    pnl_pct=Decimal("-0.01"),
                    evaluated_at=datetime(2026, 6, 1, tzinfo=UTC),
                    entry_price_used=Decimal("100"),
                    exit_price_used=Decimal("99"),
                )
            )
        db.commit()

    response = TestClient(create_app()).get(
        "/api/portfolio/discovery",
        params={"user_id": "ariel"},
    )

    assert response.status_code == 200
    government = next(
        source
        for source in response.json()["sources"]
        if source["key"] == "gov_contracts"
    )
    scorecard = government["scorecard"]
    assert scorecard["horizons"]["180d"]["scored_outcomes"] == 50
    assert scorecard["horizons"]["180d"]["win_rate"] == 0.0
    assert scorecard["horizons"]["180d"][
        "always_long_same_tickers_win_rate"
    ] == 0.0
    assert scorecard["funnel_context_enabled"] is False
    assert "does not beat always-long" in scorecard["kill_reason"]
    engine.dispose()


def test_discovery_counts_quarantined_government_contract_ticker(
    tmp_path,
    monkeypatch,
):
    engine, factory = _discovery_db(tmp_path, monkeypatch)
    _set_signal_streams_enabled(monkeypatch, True)
    with factory() as db:
        db.add(ScanState(
            user_id="ariel",
            ticker="PLTR",
            status="quarantined",
            quarantine_reason="failed-liquidity",
            nomination_evidence_json=json.dumps({
                "stream": "gov_contracts",
                "dedup_key": "usaspending:PLTR",
                "evidence": {"market_cap": 250_000_000_000},
            }),
        ))
        db.commit()

    response = TestClient(create_app()).get(
        "/api/portfolio/discovery",
        params={"user_id": "ariel"},
    )
    assert response.status_code == 200
    sources = response.json()["sources"]
    assert [source["key"] for source in sources] == [
        "attention",
        "gov_contracts",
        "growth",
        "momentum",
    ]
    government = next(
        source for source in sources if source["key"] == "gov_contracts"
    )
    assert {
        key: value for key, value in government.items() if key != "scorecard"
    } == {
        "key": "gov_contracts",
        "label": "Government contracts",
        "tracked_count": 1,
        "active_count": 0,
        "quarantined_count": 1,
        "dropped_stale_count": 0,
    }
    assert government["scorecard"]["scored_outcomes"] == 0
    engine.dispose()


def test_discovery_omits_disabled_government_contract_source(
    tmp_path,
    monkeypatch,
):
    engine, factory = _discovery_db(tmp_path, monkeypatch)
    _set_signal_streams_enabled(monkeypatch, False)
    with factory() as db:
        db.add(ScanState(
            user_id="ariel",
            ticker="PLTR",
            status="quarantined",
            nomination_evidence_json=json.dumps({
                "stream": "gov_contracts",
                "dedup_key": "usaspending:PLTR",
                "evidence": {},
            }),
        ))
        db.commit()

    response = TestClient(create_app()).get(
        "/api/portfolio/discovery",
        params={"user_id": "ariel"},
    )
    assert response.status_code == 200
    assert [source["key"] for source in response.json()["sources"]] == [
        "attention",
        "growth",
        "momentum",
    ]
    engine.dispose()


def test_discovery_stage_counts_exclude_dropped_rows_with_cached_verdicts(
    tmp_path,
    monkeypatch,
):
    engine, factory = _discovery_db(tmp_path, monkeypatch)
    estimator = EstimatorVerdict(
        ticker="ACTIVE",
        go=True,
        conviction="MED",
        sentiment=0.6,
        one_line="active candidate",
    )
    fleet = FleetPick(
        ticker="ACTIVE",
        conviction="HIGH",
        thesis_md="active thesis",
        verdict="BUY",
        cites=(),
    )
    with factory() as db:
        db.add_all([
            ScanState(
                user_id="ariel",
                ticker="ACTIVE",
                status="active",
                estimator_json=_verdict_to_json(estimator),
                fleet_json=_pick_to_json(fleet),
            ),
            ScanState(
                user_id="ariel",
                ticker="DROPPED",
                status="dropped",
                estimator_json=_verdict_to_json(EstimatorVerdict(
                    ticker="DROPPED",
                    go=True,
                    conviction="HIGH",
                    sentiment=0.9,
                    one_line="cached historical estimate",
                )),
                fleet_json=_pick_to_json(FleetPick(
                    ticker="DROPPED",
                    conviction="HIGH",
                    thesis_md="cached historical research",
                    verdict="BUY",
                    cites=(),
                )),
            ),
        ])
        db.commit()

    response = TestClient(create_app()).get(
        "/api/portfolio/discovery",
        params={"user_id": "ariel"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["stages"]["estimated"] == 1
    assert body["stages"]["estimator_go"] == 1
    assert body["stages"]["fleet_graded"] == 1
    assert body["stages"]["fleet_buy"] == 1
    dropped = next(row for row in body["candidates"] if row["ticker"] == "DROPPED")
    assert dropped["estimator"]["go"] is True
    assert dropped["fleet"]["verdict"] == "BUY"
    engine.dispose()


def test_discovery_counts_only_latest_open_proposal_per_ticker_case_insensitively(
    tmp_path,
    monkeypatch,
):
    engine, factory = _discovery_db(tmp_path, monkeypatch)
    with factory() as db:
        db.add_all([
            ScanState(user_id="ariel", ticker="CMPS", status="active"),
            ScanState(user_id="ariel", ticker="ONCE", status="active"),
            Proposal(
                user_id="ariel",
                ticker="cmps",
                action="buy",
                tier="T2",
                status="awaiting_human",
                created_at=datetime(2026, 7, 9, tzinfo=UTC),
            ),
            Proposal(
                user_id="ariel",
                ticker="cmps",
                action="buy",
                tier="T2",
                status="rejected",
                created_at=datetime(2026, 7, 10, tzinfo=UTC),
            ),
            Proposal(
                user_id="ariel",
                ticker="once",
                action="buy",
                tier="T2",
                status="cooling",
                created_at=datetime(2026, 7, 9, tzinfo=UTC),
            ),
            Proposal(
                user_id="ariel",
                ticker="once",
                action="buy",
                tier="T2",
                status="approved",
                created_at=datetime(2026, 7, 10, tzinfo=UTC),
            ),
        ])
        db.commit()

    response = TestClient(create_app()).get(
        "/api/portfolio/discovery",
        params={"user_id": "ariel"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["stages"]["open_trade_proposals"] == 1
    cmps = next(row for row in body["candidates"] if row["ticker"] == "CMPS")
    once = next(row for row in body["candidates"] if row["ticker"] == "ONCE")
    assert cmps["latest_trade_proposal"]["status"] == "rejected"
    assert once["latest_trade_proposal"]["status"] == "approved"
    engine.dispose()


def test_discovery_defensively_ignores_malformed_persisted_json(
    tmp_path,
    monkeypatch,
):
    engine, _factory = _discovery_db(tmp_path, monkeypatch)
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
        conn.exec_driver_sql(
            """
            INSERT INTO trend_scan_state (
                user_id, ticker, last_score, status, rank, quarantine_reason,
                radar_fingerprint, nomination_evidence_json, estimator_json,
                fleet_json
            ) VALUES (
                'ariel', 'BROKEN', 1.0, 'active', 1, '',
                'not-a-fingerprint', '{', '{', '{'
            )
            """
        )
        conn.exec_driver_sql(
            """
            INSERT INTO trend_scan_state (
                user_id, ticker, last_score, status, rank, quarantine_reason,
                radar_fingerprint, nomination_evidence_json, estimator_json,
                fleet_json
            ) VALUES (
                'ariel', 'WRONGSHAPE', 0.5, 'active', 2, '',
                's=0.5|f=|l=low', '[]', 'null', '[]'
            )
            """
        )

    client = TestClient(create_app())
    response = client.get("/api/portfolio/discovery", params={"user_id": "ariel"})
    assert response.status_code == 200
    broken = response.json()["candidates"][0]
    assert broken["source_keys"] == []
    assert broken["estimator"] is None
    assert broken["fleet"] is None
    wrong_shape = response.json()["candidates"][1]
    assert wrong_shape["estimator"] is None
    assert wrong_shape["fleet"] is None
    engine.dispose()


def test_discovery_funnel_loop_tick_runs_funnel(monkeypatch):
    import argosy.orchestrator.loops.discovery_funnel_loop as dfl

    called = {}

    async def fake_run(user_id, *, force=False, now=None):
        called["user_id"] = user_id
        called["force"] = force
        return FunnelResult(picks=[FleetPick("PLTR", "HIGH", "t", "BUY", ())],
                            estimated=[], radar=[], last_refreshed_at="t")
    monkeypatch.setattr(dfl, "run_funnel", fake_run)
    loop = dfl.DiscoveryFunnelLoop(user_id="ariel")
    out = asyncio.run(loop.tick())
    assert called["user_id"] == "ariel"
    assert called["force"] is False           # daily refresh is SMART (not force)
    assert out["picks"] == 1
