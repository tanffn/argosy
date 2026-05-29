"""Tests for the generic allocation Accept/Defer/list routes.

Sprint commit #6b — argosy/api/routes/allocation.py mounts at
/api/proposals/allocation/{accept,defer,actions}. Generalizes the
existing /windfall/{accept,defer,actions} pattern over the
action_source discriminator from migration 0041.
"""
from __future__ import annotations


PAYLOAD = {
    "user_id": "ariel",
    "action_source": "unallocated_cash",
    "source_detected_at": "2026-05-29T00:00:00",
    "source_ref": '{"snapshot_date":"2026-05-29","horizon":"long","asset_class":"Growth","instrument":"QQQM"}',
    "horizon": "long",
    "asset_class": "Growth",
    "instrument": "QQQM",
    "amount_usd": 12345.67,
    "rationale": "Closes the Growth plan-target gap.",
    "closes_delta_usd": 12345.67,
    "confidence": "high",
}


def _ensure_user(client_with_db, user_id: str = "ariel") -> None:
    from argosy.state.models import User
    SF = client_with_db.app.state.session_factory
    with SF() as s:
        if s.get(User, user_id) is None:
            s.add(User(id=user_id))
            s.commit()


class TestAllocationAccept:
    def test_accept_persists_row(self, client_with_db):
        _ensure_user(client_with_db)
        r = client_with_db.post(
            "/api/proposals/allocation/accept", json=PAYLOAD,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["decided_status"] == "accepted"
        assert body["id"] > 0
        assert body["due_date"] is None


class TestAllocationDefer:
    def test_defer_with_due_date(self, client_with_db):
        _ensure_user(client_with_db)
        payload = {**PAYLOAD, "due_date": "2026-06-15"}
        r = client_with_db.post(
            "/api/proposals/allocation/defer", json=payload,
        )
        assert r.status_code == 200, r.text
        assert r.json()["decided_status"] == "deferred"
        assert r.json()["due_date"] == "2026-06-15"


class TestAllocationActionsList:
    def test_list_filters_by_action_source(self, client_with_db):
        _ensure_user(client_with_db)
        # One unallocated_cash accept
        client_with_db.post("/api/proposals/allocation/accept", json=PAYLOAD)
        # One windfall accept (via the legacy route — same table)
        windfall_payload = {
            "user_id": "ariel",
            "event_detected_at": "2026-05-28T16:48:12",
            "event_source_tsv": "Family Finances Status - 26 May.tsv",
            "horizon": "long",
            "asset_class": "Growth",
            "instrument": "SCHG",
            "amount_usd": 20000,
            "rationale": "windfall test",
            "closes_delta_usd": 20000,
            "confidence": "high",
        }
        client_with_db.post(
            "/api/retirement/windfall/accept", json=windfall_payload,
        )

        # No filter → both rows surface.
        r_all = client_with_db.get(
            "/api/proposals/allocation/actions?user_id=ariel",
        )
        assert r_all.status_code == 200
        assert len(r_all.json()["actions"]) == 2

        # Filter to unallocated_cash → just one.
        r_filt = client_with_db.get(
            "/api/proposals/allocation/actions"
            "?user_id=ariel&action_source=unallocated_cash",
        )
        assert r_filt.status_code == 200
        rows = r_filt.json()["actions"]
        assert len(rows) == 1
        assert rows[0]["instrument"] == "QQQM"
        assert rows[0]["action_source"] == "unallocated_cash"

    def test_list_filters_by_source_ref(self, client_with_db):
        _ensure_user(client_with_db)
        client_with_db.post("/api/proposals/allocation/accept", json=PAYLOAD)
        # Same snapshot, different instrument → different source_ref.
        other = {
            **PAYLOAD,
            "source_ref": '{"snapshot_date":"2026-05-29","horizon":"long","asset_class":"Growth","instrument":"SCHG"}',
            "instrument": "SCHG",
        }
        client_with_db.post("/api/proposals/allocation/accept", json=other)

        # Specific source_ref → exactly one row.
        r = client_with_db.get(
            "/api/proposals/allocation/actions"
            "?user_id=ariel"
            "&action_source=unallocated_cash"
            f"&source_ref={PAYLOAD['source_ref']}",
        )
        assert r.status_code == 200
        rows = r.json()["actions"]
        assert len(rows) == 1
        assert rows[0]["instrument"] == "QQQM"

    def test_returns_empty_for_unknown_user(self, client_with_db):
        r = client_with_db.get(
            "/api/proposals/allocation/actions?user_id=nobody",
        )
        assert r.status_code == 200
        assert r.json()["actions"] == []


class TestActionSourceValidation:
    def test_unknown_action_source_rejected_422(self, client_with_db):
        _ensure_user(client_with_db)
        bad = {**PAYLOAD, "action_source": "not_a_real_source"}
        r = client_with_db.post(
            "/api/proposals/allocation/accept", json=bad,
        )
        # Pydantic Literal → 422 before the DB CHECK.
        assert r.status_code == 422

    def test_known_action_sources_all_accepted(self, client_with_db):
        _ensure_user(client_with_db)
        for source in (
            "windfall",
            "unallocated_cash",
            "monitor_drift",
            "rebalance",
            "life_event",
            "manual",
        ):
            payload = {
                **PAYLOAD,
                "action_source": source,
                # Vary source_ref so the partial-unique index doesn't trip.
                "source_ref": f'{{"test":"{source}"}}',
            }
            r = client_with_db.post(
                "/api/proposals/allocation/accept", json=payload,
            )
            assert r.status_code == 200, (
                f"action_source={source!r} should be accepted; got {r.status_code} {r.text}"
            )
