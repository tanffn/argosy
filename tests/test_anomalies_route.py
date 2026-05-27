"""EX2 /api/anomalies/* route smoke tests.

Covers:
  - GET /api/anomalies/latest with no rows → 200 + null.
  - GET /api/anomalies/latest with rows → returns most-recent row.
  - GET /api/anomalies/{id} happy path → full report.
  - GET /api/anomalies/{id} unknown id → 404.
  - GET /api/anomalies/{id} cross-tenant access → 404 (never reveals
    existence cross-tenant).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from argosy.state.models import AnomalyReport, User


@pytest.fixture
def seeded_user_and_reports(client_with_db):
    """Insert two AnomalyReport rows for user 'ariel' (older + newer)
    plus one for a different user. Returns the IDs as a dict so each
    test can drive against specific rows.
    """
    SessionLocal = client_with_db.app.state.session_factory
    s: Session = SessionLocal()
    try:
        if s.get(User, "ariel") is None:
            s.add(User(id="ariel", plan="free"))
        if s.get(User, "intruder") is None:
            s.add(User(id="intruder", plan="free"))
        s.commit()

        older = AnomalyReport(
            user_id="ariel",
            triggered_by="daily",
            triggered_at=datetime(2026, 5, 25, 7, 0, tzinfo=UTC),
            source_statement_id=None,
            report_json=json.dumps({
                "anomalies": [],
                "watchlist_status": [
                    {"name": "discount_bank_card_2923_fee_waiver",
                     "state": "NORMAL", "last_evidence": "—"},
                ],
                "cited_sources": [],
            }),
            severity_summary_json=json.dumps(
                {"RED": 0, "AMBER": 0, "YELLOW": 0}
            ),
        )
        newer = AnomalyReport(
            user_id="ariel",
            triggered_by="event",
            triggered_at=datetime(2026, 5, 27, 6, 30, tzinfo=UTC),
            source_statement_id=None,
            report_json=json.dumps({
                "anomalies": [{
                    "severity": "RED",
                    "watchlist_entry_name": "discount_bank_card_2923_fee_waiver",
                    "observation": "Discount line missing in May 2026.",
                    "last_seen": "2026-04-30",
                    "suggested_action": "Call Discount Bank.",
                }],
                "watchlist_status": [
                    {"name": "discount_bank_card_2923_fee_waiver",
                     "state": "ALERT",
                     "last_evidence": "עמלת כרטיס ₪12.50 with no matching discount"},
                ],
                "cited_sources": ["statement:1", "watchlist:discount_bank_card_2923_fee_waiver"],
            }),
            severity_summary_json=json.dumps(
                {"RED": 1, "AMBER": 0, "YELLOW": 0}
            ),
        )
        other_user = AnomalyReport(
            user_id="intruder",
            triggered_by="manual",
            triggered_at=datetime(2026, 5, 27, 8, 0, tzinfo=UTC),
            source_statement_id=None,
            report_json=json.dumps({"anomalies": [], "watchlist_status": []}),
            severity_summary_json=json.dumps(
                {"RED": 0, "AMBER": 0, "YELLOW": 0}
            ),
        )
        s.add_all([older, newer, other_user])
        s.commit()
        ids = {
            "older": older.id,
            "newer": newer.id,
            "other_user": other_user.id,
        }
    finally:
        s.close()
    return ids


def test_get_latest_when_no_rows_returns_null(client_with_db):
    """No anomaly reports for this user → endpoint returns 200 + null."""
    # Seed only the User row so the FK doesn't reject — but no anomaly_reports.
    SessionLocal = client_with_db.app.state.session_factory
    with SessionLocal() as s:
        if s.get(User, "ariel") is None:
            s.add(User(id="ariel", plan="free"))
            s.commit()

    resp = client_with_db.get("/api/anomalies/latest?user_id=ariel")
    assert resp.status_code == 200, resp.text
    assert resp.json() is None


def test_get_latest_returns_most_recent(client_with_db, seeded_user_and_reports):
    """The most recent row by triggered_at is returned."""
    ids = seeded_user_and_reports
    resp = client_with_db.get("/api/anomalies/latest?user_id=ariel")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body is not None
    assert body["id"] == ids["newer"]
    assert body["triggered_by"] == "event"
    assert body["severity_summary"] == {"RED": 1, "AMBER": 0, "YELLOW": 0}
    # report_json is parsed into a dict.
    assert body["report"]["anomalies"][0]["severity"] == "RED"
    assert (
        body["report"]["anomalies"][0]["watchlist_entry_name"]
        == "discount_bank_card_2923_fee_waiver"
    )


def test_get_by_id_returns_full_report(client_with_db, seeded_user_and_reports):
    """GET /api/anomalies/{id} returns the row's full report."""
    ids = seeded_user_and_reports
    resp = client_with_db.get(
        f"/api/anomalies/{ids['older']}?user_id=ariel"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == ids["older"]
    assert body["triggered_by"] == "daily"
    assert body["severity_summary"] == {"RED": 0, "AMBER": 0, "YELLOW": 0}
    assert body["report"]["watchlist_status"][0]["state"] == "NORMAL"


def test_get_by_id_unknown_returns_404(client_with_db, seeded_user_and_reports):
    """Unknown report id → 404."""
    resp = client_with_db.get("/api/anomalies/99999?user_id=ariel")
    assert resp.status_code == 404


def test_get_by_id_cross_tenant_returns_404(
    client_with_db, seeded_user_and_reports
):
    """A row owned by user 'intruder' must 404 when requested by 'ariel'.

    Never reveals existence cross-tenant — same pattern as the
    fleet-self-review route.
    """
    ids = seeded_user_and_reports
    resp = client_with_db.get(
        f"/api/anomalies/{ids['other_user']}?user_id=ariel"
    )
    assert resp.status_code == 404
