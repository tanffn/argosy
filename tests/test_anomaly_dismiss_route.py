"""Sprint #2 commit #11 — POST /api/anomaly/dismiss/{id} tests.

Covers:
  * Happy path: open row → flipped to ``resolved`` + ``resolved_at`` stamped.
  * Cross-tenant 404 (never reveals existence).
  * Idempotent: re-dismissing a resolved row returns its state unchanged.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from argosy.state.models import ExpenseReviewQueue, User


@pytest.fixture
def seeded_dismiss(client_with_db):
    SessionLocal = client_with_db.app.state.session_factory
    s: Session = SessionLocal()
    try:
        for uid in ("ariel", "intruder"):
            if s.get(User, uid) is None:
                s.add(User(id=uid, plan="free"))
        s.commit()
        open_row = ExpenseReviewQueue(
            user_id="ariel",
            kind="a1_category_outlier",
            status="open",
            payload_json=json.dumps({"merchant_normalized": "x", "amount_nis": 500}),
            related_tx_id=10,
            materiality="warning",
            bucket="amount",
            created_at=datetime.now(timezone.utc),
        )
        intruder_row = ExpenseReviewQueue(
            user_id="intruder",
            kind="a1_category_outlier",
            status="open",
            payload_json=json.dumps({"merchant_normalized": "x"}),
            related_tx_id=11,
            materiality="warning",
            bucket="amount",
            created_at=datetime.now(timezone.utc),
        )
        s.add_all([open_row, intruder_row])
        s.commit()
        ids = {"open": open_row.id, "intruder": intruder_row.id}
    finally:
        s.close()
    return ids


def test_dismiss_happy_path_flips_status(client_with_db, seeded_dismiss):
    ids = seeded_dismiss
    resp = client_with_db.post(
        f"/api/anomaly/dismiss/{ids['open']}?user_id=ariel"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == ids["open"]
    assert body["status"] == "resolved"
    assert body["resolved_at"]  # stamped (non-empty iso string)

    # DB confirms the flip.
    SessionLocal = client_with_db.app.state.session_factory
    with SessionLocal() as s:
        row = s.get(ExpenseReviewQueue, ids["open"])
        assert row is not None
        assert row.status == "resolved"
        assert row.resolved_at is not None


def test_dismiss_cross_tenant_returns_404(client_with_db, seeded_dismiss):
    """A row owned by 'intruder' must 404 for 'ariel'."""
    ids = seeded_dismiss
    resp = client_with_db.post(
        f"/api/anomaly/dismiss/{ids['intruder']}?user_id=ariel"
    )
    assert resp.status_code == 404


def test_dismiss_is_idempotent_on_already_resolved(client_with_db, seeded_dismiss):
    """Re-dismissing a resolved row returns its state without re-stamping."""
    ids = seeded_dismiss
    first = client_with_db.post(
        f"/api/anomaly/dismiss/{ids['open']}?user_id=ariel"
    )
    assert first.status_code == 200
    first_resolved_at = first.json()["resolved_at"]

    second = client_with_db.post(
        f"/api/anomaly/dismiss/{ids['open']}?user_id=ariel"
    )
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["status"] == "resolved"
    # The resolved_at MUST equal the first stamp (no second clobber).
    assert body["resolved_at"] == first_resolved_at
