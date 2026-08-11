"""How-our-calls-did surfacing in the FM greeting (decision-learning loop).

Seeds deep-decision-verdict-sourced predictions + graded outcomes and
asserts the greeting's ``how_our_calls_did`` section renders wins as
"good call" and misses as "miss" — keyed to the right subject/verdict —
while ungraded/open predictions are excluded and any read failure omits
the section without sinking the greeting.

Pattern mirrors ``tests/test_predictions_reliability.py``: per-test
in-memory SQLite at alembic head; ORM-core seeds so the reader sees
exactly what the migration set up.

Run:
    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \\
        tests/test_home_greeting_calls_did.py -q --timeout=180
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session, sessionmaker

from argosy.services import home_greeting
from argosy.services.home_greeting import (
    _how_our_calls_did,
    _verdict_call_line,
    build_greeting,
)
from argosy.services.predictions.reliability import (
    DEEP_DECISION_VERDICT_SOURCE,
    recent_verdict_call_outcomes,
)
from argosy.state.models import Prediction, PredictionOutcome

_EVENT_AT = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)
_EVAL_AT = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def sync_session(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    from argosy.config import get_settings, reload_settings

    reload_settings()
    sync_url = get_settings().database_url.replace("+aiosqlite", "")
    db_path = sync_url.replace("sqlite:///", "")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    command.upgrade(Config("alembic.ini"), "head")

    engine = sa.create_engine(sync_url, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys = ON"))
        conn.execute(
            sa.text(
                "INSERT INTO users (id, plan, created_at) "
                "VALUES ('ariel', 'free', '2026-01-01 00:00:00+00:00')"
            )
        )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


_COUNTER = [0]


def _seed_verdict(
    session: Session,
    *,
    subject: str,
    verdict: str,
    direction: str,
    verdict_id: int,
    with_outcome: bool = True,
    outcome_kind: str = "hit_target",
    pnl_pct: float | None = 0.06,
    entry: float | None = 100.0,
    exit_: float | None = 94.0,
    user_id: str = "ariel",
) -> Prediction:
    _COUNTER[0] += 1
    pred = Prediction(
        user_id=user_id,
        source=DEEP_DECISION_VERDICT_SOURCE,
        source_ref=json.dumps(
            {
                "verdict_id": verdict_id,
                "subject": subject,
                "verdict": verdict,
                "kind": "deep_decision_verdict",
            }
        ),
        ticker=subject,
        direction=direction,
        timeframe_days=30,
        message_id=f"v1|predictions|deep_decision_verdict|{verdict_id}.{_COUNTER[0]}",
        event_at=_EVENT_AT,
        evaluation_due_at=_EVENT_AT + timedelta(days=30),
        evaluation_method="fixed_lookahead_30d",
        archived=0,
    )
    session.add(pred)
    session.flush()
    if with_outcome:
        from decimal import Decimal

        session.add(
            PredictionOutcome(
                prediction_id=pred.id,
                evaluation_method="fixed_lookahead_30d",
                outcome_kind=outcome_kind,
                pnl_pct=Decimal(str(pnl_pct)) if pnl_pct is not None else None,
                evaluated_at=_EVAL_AT,
                entry_price_used=Decimal(str(entry)) if entry is not None else None,
                exit_price_used=Decimal(str(exit_)) if exit_ is not None else None,
                exit_trigger_date=date(2026, 9, 7),
            )
        )
        session.flush()
    return pred


def test_sell_that_fell_is_a_good_call(sync_session) -> None:
    # SELL NVDA (short); NVDA fell 100 -> 94 = -6%, pnl +6% (call was right).
    _seed_verdict(
        sync_session,
        subject="NVDA",
        verdict="SELL",
        direction="short",
        verdict_id=101,
        outcome_kind="hit_target",
        pnl_pct=0.06,
        entry=100.0,
        exit_=94.0,
    )
    section = _how_our_calls_did(sync_session, "ariel")
    assert len(section) == 1
    row = section[0]
    assert row["subject"] == "NVDA"
    assert row["verdict"] == "SELL"
    assert row["grade"] == "win"
    assert row["move_pct"] == pytest.approx(-6.0, abs=1e-6)
    assert "SELL NVDA" in row["headline"]
    assert "-6%" in row["headline"]
    assert "good call" in row["headline"]
    assert row["as_of"] == "2026-09-07"


def test_sell_that_rose_is_a_miss(sync_session) -> None:
    # SELL AAPL (short); AAPL ROSE 100 -> 108 = +8%, pnl -8% (call was wrong).
    _seed_verdict(
        sync_session,
        subject="AAPL",
        verdict="SELL",
        direction="short",
        verdict_id=202,
        outcome_kind="expired_negative",
        pnl_pct=-0.08,
        entry=100.0,
        exit_=108.0,
    )
    section = _how_our_calls_did(sync_session, "ariel")
    assert len(section) == 1
    row = section[0]
    assert row["subject"] == "AAPL"
    assert row["grade"] == "miss"
    assert row["move_pct"] == pytest.approx(8.0, abs=1e-6)
    assert "+8%" in row["headline"]
    assert "miss" in row["headline"]
    assert "good call" not in row["headline"]


def test_hold_that_held_through_upmove_worked(sync_session) -> None:
    # HOLD (neutral); price rose = holding was fine.
    _seed_verdict(
        sync_session,
        subject="MSFT",
        verdict="HOLD",
        direction="neutral",
        verdict_id=303,
        outcome_kind="expired_positive",
        pnl_pct=0.04,
        entry=100.0,
        exit_=104.0,
    )
    row = _how_our_calls_did(sync_session, "ariel")[0]
    assert row["grade"] == "win"
    assert "+4%" in row["headline"]
    assert "the hold worked" in row["headline"]


def test_ungraded_and_unparseable_excluded(sync_session) -> None:
    # Graded win (kept).
    _seed_verdict(
        sync_session, subject="NVDA", verdict="SELL", direction="short",
        verdict_id=1, outcome_kind="hit_target", pnl_pct=0.05,
    )
    # Open/ungraded — no outcome row at all.
    _seed_verdict(
        sync_session, subject="TSLA", verdict="BUY", direction="long",
        verdict_id=2, with_outcome=False,
    )
    # Graded-but-unparseable — excluded (not a real grade).
    _seed_verdict(
        sync_session, subject="AMD", verdict="SELL", direction="short",
        verdict_id=3, outcome_kind="unparseable", pnl_pct=None,
        entry=None, exit_=None,
    )
    subjects = {r["subject"] for r in _how_our_calls_did(sync_session, "ariel")}
    assert subjects == {"NVDA"}


def test_reader_caps_and_orders_by_recency(sync_session) -> None:
    for i in range(7):
        _seed_verdict(
            sync_session, subject=f"T{i}", verdict="SELL", direction="short",
            verdict_id=500 + i, outcome_kind="hit_target", pnl_pct=0.03,
        )
    outcomes = recent_verdict_call_outcomes(sync_session, "ariel", limit=5)
    assert len(outcomes) == 5


def test_read_failure_omits_section_not_raises(sync_session, monkeypatch) -> None:
    def _boom(*_a, **_k):
        raise RuntimeError("reader exploded")

    monkeypatch.setattr(
        "argosy.services.predictions.reliability.recent_verdict_call_outcomes",
        _boom,
    )
    # Section swallows the error -> empty list.
    assert _how_our_calls_did(sync_session, "ariel") == []


def test_build_greeting_exposes_section(sync_session) -> None:
    _seed_verdict(
        sync_session, subject="NVDA", verdict="SELL", direction="short",
        verdict_id=901, outcome_kind="hit_target", pnl_pct=0.06,
        entry=100.0, exit_=94.0,
    )
    greeting = build_greeting(sync_session, "ariel", now=_EVAL_AT)
    assert "how_our_calls_did" in greeting
    assert isinstance(greeting["how_our_calls_did"], list)
    assert greeting["how_our_calls_did"][0]["subject"] == "NVDA"
    assert "good call" in greeting["how_our_calls_did"][0]["headline"]


def test_build_greeting_survives_reader_failure(sync_session, monkeypatch) -> None:
    monkeypatch.setattr(
        "argosy.services.predictions.reliability.recent_verdict_call_outcomes",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    greeting = build_greeting(sync_session, "ariel", now=_EVAL_AT)
    assert greeting["how_our_calls_did"] == []


def test_verdict_call_line_formats_plainly() -> None:
    from argosy.services.predictions.reliability import VerdictCallOutcome

    o = VerdictCallOutcome(
        verdict_id=7, subject="NVDA", verdict="SELL", direction="short",
        outcome_kind="hit_target", pnl_pct=0.06, price_move_pct=-6.0,
        event_at=_EVENT_AT, evaluated_at=_EVAL_AT,
    )
    line = _verdict_call_line(o)
    assert line.startswith("SELL NVDA (Aug 8):")
    assert "-6% since" in line
    assert line.endswith("good call")
