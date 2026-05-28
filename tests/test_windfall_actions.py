"""Tests for the windfall_actions persistence: Accept / Defer / list endpoints.

Closes user-guide Hole #2 -- the WindfallCard's Accept/Defer buttons
were previously disabled because there was no persistence shape for
them. These tests pin the contract:

  - POST /windfall/accept persists a row with status='accepted'
  - POST /windfall/defer persists a row with status='deferred' +
    optional due_date
  - GET /windfall/actions filters by user_id + optional event
"""
from __future__ import annotations

from datetime import date, datetime, timezone


PROPOSAL_PAYLOAD = {
    "user_id": "ariel",
    "event_detected_at": "2026-05-28T16:48:12.054167",
    "event_source_tsv": "Family Finances Status - 26 May.tsv",
    "horizon": "long",
    "asset_class": "Growth",
    "instrument": "QQQM",
    "amount_usd": 30383.82,
    "rationale": "Closes the Growth plan-target gap ($132K under target). Preferred instrument: QQQM (already in your portfolio).",
    "closes_delta_usd": 30383.82,
    "confidence": "high",
}


def _ensure_user(client_with_db, user_id="ariel"):
    """Seed the user row so the FK constraint on windfall_actions.user_id passes."""
    from argosy.state.models import User
    SessionLocal = client_with_db.app.state.session_factory
    with SessionLocal() as s:
        if s.get(User, user_id) is None:
            s.add(User(id=user_id))
            s.commit()


class TestWindfallAccept:
    def test_accept_persists_row(self, client_with_db):
        _ensure_user(client_with_db)
        resp = client_with_db.post(
            "/api/retirement/windfall/accept",
            json=PROPOSAL_PAYLOAD,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["decided_status"] == "accepted"
        assert body["id"] > 0
        assert body["due_date"] is None

    def test_accept_then_list_returns_the_row(self, client_with_db):
        _ensure_user(client_with_db)
        client_with_db.post(
            "/api/retirement/windfall/accept", json=PROPOSAL_PAYLOAD,
        )
        list_resp = client_with_db.get(
            "/api/retirement/windfall/actions?user_id=ariel",
        )
        assert list_resp.status_code == 200
        actions = list_resp.json()["actions"]
        assert len(actions) == 1
        assert actions[0]["decided_status"] == "accepted"
        assert actions[0]["instrument"] == "QQQM"
        assert actions[0]["amount_usd"] == 30383.82


class TestWindfallDefer:
    def test_defer_with_due_date(self, client_with_db):
        _ensure_user(client_with_db)
        payload = {**PROPOSAL_PAYLOAD, "due_date": "2026-06-15"}
        resp = client_with_db.post(
            "/api/retirement/windfall/defer", json=payload,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["decided_status"] == "deferred"
        assert body["due_date"] == "2026-06-15"

    def test_defer_without_due_date(self, client_with_db):
        _ensure_user(client_with_db)
        resp = client_with_db.post(
            "/api/retirement/windfall/defer", json=PROPOSAL_PAYLOAD,
        )
        assert resp.status_code == 200
        assert resp.json()["due_date"] is None
        assert resp.json()["decided_status"] == "deferred"


class TestWindfallActionsList:
    def test_filter_by_event(self, client_with_db):
        _ensure_user(client_with_db)
        # Accept on event A
        client_with_db.post(
            "/api/retirement/windfall/accept", json=PROPOSAL_PAYLOAD,
        )
        # Defer on event B (different detected_at + source_tsv)
        other_event = {
            **PROPOSAL_PAYLOAD,
            "event_detected_at": "2026-04-28T10:00:00",
            "event_source_tsv": "Family Finances Status - 26 Apr.tsv",
            "instrument": "SCHG",
            "amount_usd": 20255.88,
        }
        client_with_db.post(
            "/api/retirement/windfall/defer", json=other_event,
        )

        # No filter -> 2 rows
        resp_all = client_with_db.get(
            "/api/retirement/windfall/actions?user_id=ariel",
        )
        assert len(resp_all.json()["actions"]) == 2

        # Filter by event A's source_tsv -> 1 row
        resp_filtered = client_with_db.get(
            "/api/retirement/windfall/actions?user_id=ariel"
            "&event_source_tsv=Family%20Finances%20Status%20-%2026%20May.tsv",
        )
        rows = resp_filtered.json()["actions"]
        assert len(rows) == 1
        assert rows[0]["instrument"] == "QQQM"

    def test_returns_empty_for_unknown_user(self, client_with_db):
        # No user seeding -- the table is just empty.
        resp = client_with_db.get(
            "/api/retirement/windfall/actions?user_id=nobody",
        )
        assert resp.status_code == 200
        assert resp.json()["actions"] == []
