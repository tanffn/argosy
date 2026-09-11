from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.inbox.service import build_inbox
from argosy.services.ingest_recommendation_router import route_ingest_recommendations
from argosy.state.models import ActionProposal, Base, ScanState, User


@pytest.fixture
def sync_session(tmp_path):
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'ingest-routing.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _recommendation(ticker: str, disposition: str) -> dict[str, str]:
    return {
        "ticker": ticker,
        "disposition": disposition,
        "rationale": f"{ticker} deserves {disposition} follow-up.",
        "next_step": "Verify the thesis with current primary sources.",
        "confidence": "MEDIUM",
    }


def test_watch_enters_argosy_list_without_cluttering_inbox(sync_session) -> None:
    sync_session.add(User(id="ingest-user", plan="free"))
    sync_session.commit()

    routed = route_ingest_recommendations(
        sync_session,
        user_id="ingest-user",
        source_kind="youtube",
        source_id="video-1",
        source_url="https://www.youtube.com/watch?v=video-1",
        recommendations=[_recommendation("TTD", "watch")],
        now=datetime(2026, 9, 11, tzinfo=UTC),
    )

    state = sync_session.get(ScanState, {"user_id": "ingest-user", "ticker": "TTD"})
    proposal = sync_session.get(ActionProposal, routed[0].watchlist_proposal_id)
    assert state is not None and state.status == "active"
    assert proposal is not None and proposal.kind == "set_watchlist"
    assert routed[0].inbox_proposal_id is None
    assert all(
        item.id != f"note:{proposal.id}"
        for item in build_inbox(sync_session, user_id="ingest-user").items
    )


def test_buy_enters_list_and_surfaces_review_action_in_inbox(sync_session) -> None:
    sync_session.add(User(id="buy-user", plan="free"))
    sync_session.commit()

    routed = route_ingest_recommendations(
        sync_session,
        user_id="buy-user",
        source_kind="youtube",
        source_id="video-2",
        source_url="https://www.youtube.com/watch?v=video-2",
        recommendations=[_recommendation("APP", "buy")],
        now=datetime(2026, 9, 11, tzinfo=UTC),
    )

    state = sync_session.get(ScanState, {"user_id": "buy-user", "ticker": "APP"})
    proposal_id = routed[0].inbox_proposal_id
    proposal = sync_session.get(ActionProposal, proposal_id)
    inbox_ids = {item.id for item in build_inbox(sync_session, user_id="buy-user").items}
    assert state is not None and state.status == "active"
    assert proposal is not None and proposal.kind == "note_only"
    assert proposal.execution_state == "proposed"
    assert f"note:{proposal_id}" in inbox_ids


def test_rerun_deduplicates_open_recommendations(sync_session) -> None:
    sync_session.add(User(id="dedup-user", plan="free"))
    sync_session.commit()
    kwargs = {
        "user_id": "dedup-user",
        "source_kind": "youtube",
        "source_id": "video-3",
        "source_url": None,
        "recommendations": [_recommendation("BSX", "buy")],
        "now": datetime(2026, 9, 11, tzinfo=UTC),
    }

    first = route_ingest_recommendations(sync_session, **kwargs)
    second = route_ingest_recommendations(sync_session, **kwargs)

    assert first[0].inbox_proposal_id == second[0].inbox_proposal_id
    rows = sync_session.query(ActionProposal).filter_by(user_id="dedup-user").all()
    assert len(rows) == 1


def test_changed_disposition_supersedes_stale_buy_inbox_item(sync_session) -> None:
    sync_session.add(User(id="changed-user", plan="free"))
    sync_session.commit()
    common = {
        "user_id": "changed-user",
        "source_kind": "youtube",
        "source_id": "video-4",
        "source_url": None,
        "now": datetime(2026, 9, 11, tzinfo=UTC),
    }

    buy = route_ingest_recommendations(
        sync_session,
        recommendations=[_recommendation("TTD", "buy")],
        **common,
    )
    watch = route_ingest_recommendations(
        sync_session,
        recommendations=[_recommendation("TTD", "watch")],
        **common,
    )

    stale = sync_session.get(ActionProposal, buy[0].inbox_proposal_id)
    current = sync_session.get(ActionProposal, watch[0].watchlist_proposal_id)
    assert stale is not None and stale.status == "superseded"
    assert stale.execution_state == "dismissed"
    assert current is not None and current.status == "open"
