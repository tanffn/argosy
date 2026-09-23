from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from argosy.orchestrator.loops.predictions_evaluator import PredictionsEvaluatorLoop
from argosy.services.predictions.clock_repair import repair_alpha_prediction_horizons
from argosy.services.predictions.evaluator import find_due_predictions
from argosy.services.predictions.retention import run_retention_pass
from argosy.services.predictions.writers import write_alpha_report_prediction
from argosy.state.models import (
    AlphaReportAnalysis,
    AuditLog,
    Base,
    EvaluationMethod,
    NewsSignal,
    Prediction,
    PredictionOutcome,
    User,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'repair.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    session.add(User(id="ariel"))
    for days in (30, 180):
        base = f"fixed_lookahead_{days}d"
        for name, version in ((base, 1), (base + "_entry_backfilled", 2)):
            session.add(EvaluationMethod(method_name=name, family="fixed_lookahead",
                                         method_version=version, scoring_contract=base))
    session.commit()
    try:
        yield session, factory
    finally:
        session.close()
        engine.dispose()


def legacy_prediction(session, ticker):
    event = datetime(2020, 1, 1, tzinfo=UTC)
    signal = NewsSignal(source="discord", source_ref=ticker, received_at=event,
                        parsed_tickers="[]", event_keywords="[]", sentiment="positive",
                        source_trust="medium", evidence_excerpt=ticker, raw_text=ticker)
    session.add(signal)
    session.flush()
    analysis = AlphaReportAnalysis(
        user_id="ariel", news_signal_id=signal.id, analyzed_at=event,
        macro_tone="mixed", macro_tone_confidence="medium", key_themes="[]",
        summary_rationale="Test provenance", cautions_json="[]", index_targets_json="{}",
        confidence_overall="medium", structural_picks_json="[]",
        ticker_signals_json=json.dumps([{"ticker": ticker, "timeframe": "long"}]),
    )
    session.add(analysis)
    session.flush()
    prediction = write_alpha_report_prediction(
        session, "ariel", analysis_id=analysis.id, news_signal_id=signal.id,
        ticker=ticker, direction="long", kind="signal", event_at=event, timeframe_days=180,
    )
    prediction.evaluation_method = "fixed_lookahead_30d"
    prediction.evaluation_due_at = event + timedelta(days=30)
    session.commit()
    return prediction, signal


def test_real_loop_rolls_back_all_repairs_when_later_provenance_is_invalid(db):
    session, factory = db
    first, _ = legacy_prediction(session, "FIRST")
    second, source = legacy_prediction(session, "SECOND")
    source.received_at += timedelta(days=1)
    session.commit()
    loop = PredictionsEvaluatorLoop(session_factory=factory)
    with pytest.raises(ValueError, match="provenance"):
        loop._run_tick_sync(datetime(2026, 9, 21, tzinfo=UTC))
    session.expire_all()
    assert session.get(Prediction, first.id).evaluation_method == "fixed_lookahead_30d"
    assert session.get(Prediction, second.id).evaluation_method == "fixed_lookahead_30d"
    assert session.scalars(select(AuditLog)).all() == []
    source.received_at -= timedelta(days=1)
    session.commit()
    assert repair_alpha_prediction_horizons(session) == {"repaired": 2}
    session.commit()
    assert repair_alpha_prediction_horizons(session) == {"repaired": 0}
    assert len(session.scalars(select(AuditLog)).all()) == 2


def test_old_checkpoint_does_not_archive_corrected_prediction_before_scoring(db):
    session, _ = db
    prediction, _ = legacy_prediction(session, "OLD")
    session.add(PredictionOutcome(prediction_id=prediction.id,
                                   evaluation_method="fixed_lookahead_30d",
                                   outcome_kind="expired_positive", pnl_pct=0.1,
                                   evaluated_at=datetime(2020, 2, 1, tzinfo=UTC)))
    session.commit()
    repair_alpha_prediction_horizons(session)
    now = datetime(2026, 9, 21, tzinfo=UTC)
    summary = run_retention_pass(session, now=now)
    assert summary.archived_count == 0
    assert prediction.id in {row.id for row in find_due_predictions(session, now=now)}
    audit = session.scalars(select(AuditLog)).one()
    receipt = json.loads(audit.payload_json)
    assert receipt["before"]["evaluation_method"] == "fixed_lookahead_30d"
    assert receipt["after"]["evaluation_method"] == "fixed_lookahead_180d"
    assert len(session.scalars(select(PredictionOutcome)).all()) == 1
