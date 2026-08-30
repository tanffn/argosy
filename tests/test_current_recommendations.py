from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from argosy.services.current_recommendations import (
    load_actionable_recommendations,
    recommendation_ids,
    recommendation_keys,
)
from argosy.state.models import Base, HoldingReview, Proposal, User

NOW = datetime(2026, 8, 26, 16, 0, tzinfo=UTC)


def _proposal(**overrides) -> Proposal:
    values = {
        "user_id": "ariel",
        "ticker": "GLUE",
        "action": "buy",
        "size_shares_or_currency": 26_000,
        "size_units": "currency",
        "tier": "T2",
        "account_class": "main",
        "status": "awaiting_human",
        "rationale_summary": "Fresh moonshot recommendation.",
        "expected_impact_json": "{}",
        "confidence": "MEDIUM",
        "source": "decision_funnel",
        "shadow": 0,
        "created_at": NOW - timedelta(hours=1),
        "updated_at": NOW - timedelta(hours=1),
        "expires_at": NOW + timedelta(days=2),
    }
    values.update(overrides)
    return Proposal(**values)


@pytest.mark.real_seam
def test_loads_only_fresh_autonomous_awaiting_human_recommendations() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="ariel"))
        db.add_all(
            [
                _proposal(ticker="NVDA", action="sell", size_units="shares", size_shares_or_currency=519, source="verdict_trigger_sweep"),
                _proposal(ticker="GLUE"),
                _proposal(ticker="NOW", source="manual"),
                _proposal(ticker="OLD", created_at=NOW - timedelta(days=8), expires_at=None),
                _proposal(ticker="SHADOW", shadow=1),
                _proposal(ticker="DONE", status="approved"),
            ]
        )
        db.commit()

        rows = load_actionable_recommendations(db, user_id="ariel", as_of=NOW)

    assert [(row["ticker"], row["action"]) for row in rows] == [
        ("NVDA", "sell"),
        ("GLUE", "buy"),
    ]
    assert recommendation_ids(rows) == sorted(row["proposal_id"] for row in rows)
    assert recommendation_keys(rows) == [
        f"proposal:{row['proposal_id']}" for row in rows
    ]
    engine.dispose()


@pytest.mark.real_seam
def test_latest_actionable_holding_review_reaches_unified_author_even_when_disputed() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="ariel"))
        db.add_all(
            [
                HoldingReview(
                    user_id="ariel", symbol="TSLA", reviewed_at=NOW - timedelta(hours=2),
                    verdict="HOLD", confidence="MED", reason="older keep",
                    evidence_json="{}", position_usd=10_000,
                    elevated_by_flag=True, outcome="hold",
                ),
                HoldingReview(
                    user_id="ariel", symbol="TSLA", reviewed_at=NOW - timedelta(hours=1),
                    verdict="TRIM", confidence="MED", reason="earnings and margin weakened",
                    evidence_json="{}", position_usd=10_000,
                    elevated_by_flag=True, outcome="held_unverified",
                ),
                # An older action for NOW must not survive its newer HOLD.
                HoldingReview(
                    user_id="ariel", symbol="NOW", reviewed_at=NOW - timedelta(hours=3),
                    verdict="SELL", confidence="LOW", reason="old reduce",
                    evidence_json="{}", position_usd=8_000,
                    elevated_by_flag=False, outcome="proposed",
                ),
                HoldingReview(
                    user_id="ariel", symbol="NOW", reviewed_at=NOW - timedelta(minutes=30),
                    verdict="HOLD", confidence="MED", reason="new evidence intact",
                    evidence_json="{}", position_usd=8_000,
                    elevated_by_flag=False, outcome="hold",
                ),
            ]
        )
        db.commit()

        rows = load_actionable_recommendations(db, user_id="ariel", as_of=NOW)

    assert len(rows) == 1
    assert rows[0]["ticker"] == "TSLA"
    assert rows[0]["action"] == "trim"
    assert rows[0]["size"] is None
    assert rows[0]["verification_status"] == "disputed"
    assert recommendation_ids(rows) == []
    assert recommendation_keys(rows) == [
        f"holding_review:{rows[0]['holding_review_id']}"
    ]
    engine.dispose()


@pytest.mark.real_seam
def test_deduped_confirmed_review_remains_a_unified_plan_input() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id="ariel"))
        db.add(HoldingReview(
            user_id="ariel",
            symbol="NVDA",
            reviewed_at=NOW,
            verdict="TRIM",
            confidence="HIGH",
            reason="confirmed concentration reduction",
            evidence_json="{}",
            position_usd=2_000_000,
            elevated_by_flag=False,
            outcome="dedup_skipped",
        ))
        db.commit()

        rows = load_actionable_recommendations(db, user_id="ariel", as_of=NOW)

    assert [(row["ticker"], row["action"], row["verification_status"]) for row in rows] == [
        ("NVDA", "trim", "confirmed")
    ]
    engine.dispose()
