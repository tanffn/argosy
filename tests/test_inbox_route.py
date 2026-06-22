"""Tests for GET /api/inbox.

Uses the sync TestClient (client_with_db) whose get_db override + session
factory share one file-backed SQLite, so rows inserted in the test are visible
to the route.
"""

from __future__ import annotations

from argosy.state.models import Proposal, User


def _seed_user(client_with_db):
    SF = client_with_db.app.state.session_factory
    with SF() as s:
        if s.get(User, "ariel") is None:
            s.add(User(id="ariel", plan="free"))
            s.commit()


def test_empty_inbox_returns_quiet_feed(client_with_db):
    _seed_user(client_with_db)
    r = client_with_db.get("/api/inbox?user_id=ariel")
    assert r.status_code == 200
    body = r.json()
    assert body["quiet"] is True
    assert body["items"] == []
    assert body["needs_you_count"] == 0
    assert body["policy_version"].startswith("inbox-pol-")
    assert "liveness" in body
    assert body["dropped"] == []  # non-debug hides dropped


def test_inbox_surfaces_an_awaiting_trade(client_with_db):
    _seed_user(client_with_db)
    SF = client_with_db.app.state.session_factory
    with SF() as s:
        s.add(
            Proposal(
                user_id="ariel",
                ticker="ZZZ",
                action="sell",
                size_shares_or_currency=5,
                size_units="shares",
                instrument="stock",
                order_type="market",
                tier="T2",
                account_class="main",
                status="awaiting_human",
                rationale_summary="Trim the concentrated position.",
                shadow=0,
            )
        )
        s.commit()
    body = client_with_db.get("/api/inbox?user_id=ariel").json()
    assert body["needs_you_count"] == 1
    item = body["items"][0]
    assert item["kind"] == "trade"
    assert "ZZZ" in item["title"]
    assert item["primary_action"]["intent"] == "approve"
    assert item["rank_reason"]  # server-computed, non-empty
    # No internal jargon leaks into the client payload.
    for leak in ("awaiting_human", "account_class", "T2", "shadow"):
        assert leak not in str(item)


def test_debug_param_exposes_signals(client_with_db):
    _seed_user(client_with_db)
    SF = client_with_db.app.state.session_factory
    with SF() as s:
        s.add(
            Proposal(
                user_id="ariel",
                ticker="ZZZ",
                action="buy",
                size_shares_or_currency=5,
                size_units="shares",
                instrument="stock",
                order_type="market",
                tier="T2",
                account_class="main",
                status="awaiting_human",
                rationale_summary="x",
                shadow=0,
            )
        )
        s.commit()
    body = client_with_db.get("/api/inbox?user_id=ariel&debug=true").json()
    assert "signals" in body["items"][0]
    assert body["items"][0]["signals"]["action"] == "buy"
