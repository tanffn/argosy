"""Tests for build_inbox — the assembly over today's sources.

Uses a focused file-backed SQLite session seeded with Proposal + ActionProposal
rows. The plan-task and cash adapters return empty without a plan/snapshot
(verified here as graceful), so this test targets the trade + note adapters,
shadow exclusion, dedupe, materiality suppression, and the liveness metadata.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.inbox.service import build_inbox
from argosy.services.inbox.types import PriorityBucket
from argosy.state.models import ActionProposal, Base, Proposal, User

_NOW = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
_TODAY = _NOW.date()


@pytest.fixture
def db(tmp_path):
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'inbox.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SF = sessionmaker(bind=engine, expire_on_commit=False)
    s = SF()
    try:
        s.add(User(id="ariel", plan="free"))
        s.commit()
        yield s
    finally:
        s.close()
        engine.dispose()


def _trade(s, **kw):
    defaults = dict(
        user_id="ariel",
        ticker="AAA",
        action="buy",
        size_shares_or_currency=10,
        size_units="shares",
        instrument="stock",
        order_type="market",
        tier="T2",
        account_class="main",
        status="awaiting_human",
        rationale_summary="Because the thesis holds.",
        shadow=0,
    )
    defaults.update(kw)
    row = Proposal(**defaults)
    s.add(row)
    s.commit()
    return row


def _note(s, **kw):
    defaults = dict(
        user_id="ariel",
        summary="A thing to look at",
        rationale_md="Some detail.",
        suggested_payload="{}",
        severity="warning",
        kind="note_only",
        status="open",
        surfaced_at=_NOW,
        expires_at=_NOW + timedelta(days=30),
    )
    defaults.update(kw)
    row = ActionProposal(**defaults)
    s.add(row)
    s.commit()
    return row


def test_empty_inbox_is_quiet(db):
    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    assert feed.quiet is True
    assert feed.items == []
    assert feed.liveness.pending_decisions == 0
    assert feed.liveness.cash_within_band is True
    assert feed.liveness.no_overdue_tasks is True
    assert feed.policy_version.startswith("inbox-pol-")


def test_shadow_proposal_never_surfaces(db):
    _trade(db, ticker="SHADOW", shadow=1, action="sell")
    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    assert feed.quiet is True


def test_non_actionable_status_excluded(db):
    _trade(db, ticker="DRAFT", status="draft")
    _trade(db, ticker="DONE", status="executed_paper")
    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    assert feed.quiet is True


def test_sell_is_risk_reduction_buy_is_opportunity_ordered(db):
    _trade(db, ticker="BUYME", action="buy")
    _trade(db, ticker="SELLME", action="sell")
    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    kinds = [(i.title, i.bucket) for i in feed.items]
    # Sell (risk reduction, bucket 2) ranks above buy (opportunity, bucket 5).
    assert kinds[0][1] == PriorityBucket.RISK_REDUCTION
    assert "SELLME" in kinds[0][0]
    assert kinds[1][1] == PriorityBucket.OPPORTUNITY
    assert "BUYME" in kinds[1][0]


def test_expiring_buy_jumps_to_top(db):
    _trade(db, ticker="SLOW", action="sell")  # risk reduction, bucket 2
    _trade(
        db,
        ticker="EXPIRING",
        action="buy",
        expires_at=_NOW + timedelta(days=1),
    )
    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    assert feed.items[0].bucket == PriorityBucket.OVERDUE_BLOCKING
    assert "EXPIRING" in feed.items[0].title
    assert "Expires in 1 day" in feed.items[0].rank_reason


def test_approved_proposal_offers_execute(db):
    _trade(db, ticker="APP", status="approved", action="buy")
    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    assert feed.items[0].primary_action.intent == "execute"
    assert feed.items[0].primary_action.requires_confirmation is True


def test_info_note_suppressed_warning_surfaces(db):
    _note(db, summary="info note", severity="info")
    _note(db, summary="warning note", severity="warning")
    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    titles = [i.title for i in feed.items]
    assert "warning note" in titles
    assert "info note" not in titles
    # The suppressed one is recorded for the debug view, not lost.
    assert any(d["reason"] == "below_materiality" for d in feed.dropped)


def test_critical_risk_note_is_risk_reduction(db):
    _note(db, summary="concentration risk", severity="critical", kind="rebalance")
    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    assert feed.items[0].bucket == PriorityBucket.RISK_REDUCTION


def test_no_internal_enums_in_client_projection(db):
    _trade(db, ticker="AAA", action="sell", tier="T3", status="awaiting_human")
    _note(db, summary="x", severity="critical", kind="concentration")
    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    blob = str(feed.to_dict())
    for leak in ("awaiting_human", "account_class", '"tier"', "T3", "shadow"):
        assert leak not in blob


def test_cash_directive_uses_canonical_engine_buy_list(db, monkeypatch):
    """The proactive cash directive surfaces the CANONICAL engine's buy list
    (core + discovery sleeve), not the detector's sleeve-less proposals."""
    import argosy.services.inbox.service as svc
    import argosy.services.unallocated_cash_detector as det

    class _Ev:
        excess_usd = 30_000.0
        headline = "Cash sits above your plan target."
        snapshot_date = "2026-06-30"
        proposals = []  # the OLD engine would supply nothing here

    canonical_rows = [
        {"instrument": "MOON", "asset_class": "High-growth potential",
         "amount_usd": 1_500.0, "tier": "high", "rationale": "discovery BUY"},
        {"instrument": "CSPX", "asset_class": "US broad-market core",
         "amount_usd": 28_500.0, "tier": "core", "rationale": "gap-fill"},
    ]
    monkeypatch.setattr(det, "detect_unallocated_cash_overage", lambda *a, **k: _Ev())
    monkeypatch.setattr(svc, "_cash_buy_list", lambda *a, **k: canonical_rows)

    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    cash = [i for i in feed.items if i.kind == "cash_deploy"]
    assert cash, "cash directive should surface when cash is over target"
    assert cash[0].body["buy_list"] == canonical_rows


def test_cash_directive_falls_back_to_detector_when_canonical_unavailable(db, monkeypatch):
    """If the canonical engine can't build (no plan / error), the item still
    renders from the detector's proposals — the inbox never blanks."""
    import argosy.services.inbox.service as svc
    import argosy.services.unallocated_cash_detector as det

    class _P:
        instrument = "AAA"
        asset_class = "equity"
        amount_usd = 100.0
        rationale = "gap"

    class _Ev:
        excess_usd = 5_000.0
        headline = "h"
        snapshot_date = None
        proposals = [_P()]

    monkeypatch.setattr(det, "detect_unallocated_cash_overage", lambda *a, **k: _Ev())
    monkeypatch.setattr(svc, "_cash_buy_list", lambda *a, **k: None)

    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    cash = [i for i in feed.items if i.kind == "cash_deploy"][0]
    assert cash.body["buy_list"] == [
        {"instrument": "AAA", "asset_class": "equity", "amount_usd": 100.0, "rationale": "gap"}
    ]


def test_debug_dict_exposes_signals_and_dropped(db):
    _note(db, summary="info note", severity="info")
    feed = build_inbox(db, user_id="ariel", today=_TODAY)
    d = feed.to_dict(debug=True)
    assert d["dropped"]  # populated in debug
    # client projection hides it
    assert feed.to_dict(debug=False)["dropped"] == []
