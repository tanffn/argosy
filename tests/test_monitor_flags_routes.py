"""Tests for the /api/retirement/monitor/flags + /acknowledge routes.

Sprint commit #17 — extended /monitor/flags to return MonitorFlag.id +
surfaced_at + payload-as-object, and added the acknowledge endpoint
the Red-Flag Strip dismiss button POSTs to.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone


def _ensure_user(client_with_db, user_id="ariel"):
    from argosy.state.models import User
    SF = client_with_db.app.state.session_factory
    with SF() as s:
        if s.get(User, user_id) is None:
            s.add(User(id=user_id))
            s.commit()


def _seed_flag(
    client_with_db,
    *,
    kind: str = "allocation_drift",
    severity: str = "warning",
    payload: dict | None = None,
    acknowledged: bool = False,
    expired: bool = False,
) -> int:
    """Insert a MonitorFlag and return its id."""
    from argosy.state.models import MonitorFlag
    SF = client_with_db.app.state.session_factory
    payload = payload or {
        "snapshot_date": "2026-05-29",
        "row_category": "Growth",
        "rel_drift": 0.14,
        "abs_drift_usd": 12000,
    }
    if kind == "mc_regression" and "fired" not in payload:
        payload["fired"] = True
    now = datetime.now(timezone.utc)
    with SF() as s:
        flag = MonitorFlag(
            user_id="ariel",
            kind=kind,
            severity=severity,
            payload=json.dumps(payload),
            surfaced_at=now,
            acknowledged_at=now if acknowledged else None,
            expires_at=now - timedelta(days=1) if expired else None,
        )
        s.add(flag)
        s.commit()
        return flag.id


class TestListMonitorFlags:
    def test_returns_active_flags_with_id_and_payload(self, client_with_db):
        _ensure_user(client_with_db)
        flag_id = _seed_flag(client_with_db)
        r = client_with_db.get(
            "/api/retirement/monitor/flags?user_id=ariel",
        )
        assert r.status_code == 200, r.text
        rows = r.json()
        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == flag_id
        assert row["kind"] == "allocation_drift"
        assert row["severity"] == "warning"
        # Payload is a nested object now (was spread before)
        assert isinstance(row["payload"], dict)
        assert row["payload"]["row_category"] == "Growth"
        # surfaced_at is an ISO string
        assert isinstance(row["surfaced_at"], str)

    def test_acknowledged_flags_excluded(self, client_with_db):
        _ensure_user(client_with_db)
        _seed_flag(client_with_db, acknowledged=True)
        r = client_with_db.get(
            "/api/retirement/monitor/flags?user_id=ariel",
        )
        assert r.status_code == 200
        assert r.json() == []

    def test_expired_flags_excluded(self, client_with_db):
        _ensure_user(client_with_db)
        _seed_flag(client_with_db, expired=True)
        r = client_with_db.get(
            "/api/retirement/monitor/flags?user_id=ariel",
        )
        assert r.status_code == 200
        assert r.json() == []

    def test_mc_regression_non_fired_excluded(self, client_with_db):
        """Baseline + no-fire anchor MC rows have fired=False — filter them out."""
        _ensure_user(client_with_db)
        _seed_flag(
            client_with_db,
            kind="mc_regression",
            severity="info",
            payload={
                "snapshot_date": "2026-05-29",
                "baseline": True,
                "fired": False,
                "curr_p_solvent": 0.82,
            },
        )
        r = client_with_db.get(
            "/api/retirement/monitor/flags?user_id=ariel",
        )
        assert r.status_code == 200
        assert r.json() == []

    def test_mc_regression_fired_included(self, client_with_db):
        _ensure_user(client_with_db)
        _seed_flag(
            client_with_db,
            kind="mc_regression",
            severity="warning",
            payload={
                "snapshot_date": "2026-05-29",
                "baseline": False,
                "fired": True,
                "prev_p_solvent": 0.82,
                "curr_p_solvent": 0.72,
                "delta_pp": -10.0,
            },
        )
        r = client_with_db.get(
            "/api/retirement/monitor/flags?user_id=ariel",
        )
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 1
        assert rows[0]["kind"] == "mc_regression"
        assert rows[0]["payload"]["delta_pp"] == -10.0


class TestAcknowledgeMonitorFlag:
    def test_acknowledge_sets_acknowledged_at(self, client_with_db):
        _ensure_user(client_with_db)
        flag_id = _seed_flag(client_with_db)
        r = client_with_db.post(
            f"/api/retirement/monitor/flags/{flag_id}/acknowledge",
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == flag_id
        assert isinstance(body["acknowledged_at"], str)

        # Subsequent list excludes the acknowledged flag.
        r2 = client_with_db.get(
            "/api/retirement/monitor/flags?user_id=ariel",
        )
        assert r2.json() == []

    def test_acknowledge_unknown_id_returns_404(self, client_with_db):
        r = client_with_db.post(
            "/api/retirement/monitor/flags/9999/acknowledge",
        )
        assert r.status_code == 404

    def test_acknowledge_is_idempotent(self, client_with_db):
        _ensure_user(client_with_db)
        flag_id = _seed_flag(client_with_db)
        r1 = client_with_db.post(
            f"/api/retirement/monitor/flags/{flag_id}/acknowledge",
        )
        r2 = client_with_db.post(
            f"/api/retirement/monitor/flags/{flag_id}/acknowledge",
        )
        assert r1.status_code == 200
        assert r2.status_code == 200
        # Second call leaves the existing acknowledged_at in place.
        assert r1.json()["acknowledged_at"] == r2.json()["acknowledged_at"]
