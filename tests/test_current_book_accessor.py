"""``load_current_book_snapshot`` — the single guarded book accessor.

Six modules were migrated off ``parse_portfolio_tsv`` (raw newest-TSV walk)
onto this accessor so they read the merged, ingest-guarded DB snapshot book
instead of a possibly stale/truncated raw file. These tests cover the three
source branches: explicit ``tsv_path`` override, an injected session, and the
internal short-lived read session.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from argosy.services import portfolio_snapshot_store as pss
from argosy.services.portfolio_snapshot_store import load_current_book_snapshot
from argosy.state.models import Base, PortfolioSnapshotRow, User


@pytest.fixture
def sync_session(tmp_path):
    """File-backed sync session with tables + a seeded user."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'book.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    session.add(User(id="ariel"))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_row(session, *, user_id: str = "ariel") -> None:
    session.add(
        PortfolioSnapshotRow(
            user_id=user_id,
            snapshot_date=date(2026, 5, 1),
            imported_at=datetime.now(timezone.utc),
            source_path="/tmp/family.tsv",
            positions_json=json.dumps(
                [
                    {
                        "location": "Leumi",
                        "currency": "NIS",
                        "asset_type": "stock",
                        "symbol": "VWRA",
                        "shares": 10.0,
                        "usd_value_k": 5.0,
                    }
                ]
            ),
            allocations_json="[]",
            nvda_sales_json="[]",
            real_estate_json="[]",
            pensions_json="[]",
            totals_json="{}",
            fx_usd_nis=3.7,
            fx_usd_eur=4.0,
            parse_warnings_json="[]",
        )
    )
    session.commit()


def test_session_branch_returns_guarded_book(sync_session):
    _seed_row(sync_session)
    snap = load_current_book_snapshot(sync_session, "ariel")
    assert snap is not None
    assert snap.snapshot_date == date(2026, 5, 1)
    assert [p.symbol for p in snap.positions] == ["VWRA"]
    assert snap.fx_usd_nis == 3.7


def test_session_branch_none_when_no_snapshot(sync_session):
    # User exists but no snapshot row persisted.
    assert load_current_book_snapshot(sync_session, "ariel") is None


def test_tsv_path_override_parses_file_not_db(monkeypatch):
    """An explicit ``tsv_path`` is parsed directly and never touches the DB."""
    sentinel = object()
    import argosy.ingest.tsv as tsv_mod

    monkeypatch.setattr(tsv_mod, "parse_portfolio_tsv", lambda p: sentinel)

    def _boom(*_a, **_k):  # DB must not be consulted on the path branch.
        raise AssertionError("DB read on tsv_path branch")

    monkeypatch.setattr(pss, "get_latest_snapshot_row", _boom)
    monkeypatch.setattr(pss, "_open_sync_read_session", _boom)

    out = load_current_book_snapshot(None, "ariel", tsv_path="whatever.tsv")
    assert out is sentinel


def test_internal_session_branch(sync_session, monkeypatch):
    """No caller session → internal read session is opened and used."""
    _seed_row(sync_session)

    from contextlib import contextmanager

    @contextmanager
    def _fake_open():
        yield sync_session

    monkeypatch.setattr(pss, "_open_sync_read_session", _fake_open)

    snap = load_current_book_snapshot(user_id="ariel")
    assert snap is not None
    assert [p.symbol for p in snap.positions] == ["VWRA"]


def test_internal_session_failure_degrades_to_none(monkeypatch):
    """Any failure in the internal-session path degrades to None, not raise."""

    def _boom():
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(pss, "_open_sync_read_session", _boom)
    assert load_current_book_snapshot(user_id="ariel") is None
