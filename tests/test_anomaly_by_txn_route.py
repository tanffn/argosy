"""Sprint #2 commit #11 — GET /api/anomaly/by-txn tests.

Covers:
  * Empty ``txn_ids`` → 200 + ``{}``.
  * Direct mapping via ``related_tx_id``.
  * Bucket-D duplicate pair: the LATER leg (payload.max_tx_id) also
    lights up even though only the EARLIER leg is in ``related_tx_id``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from argosy.state.models import ExpenseReviewQueue, User


def _row(
    user_id: str,
    *,
    kind: str,
    materiality: str,
    related_tx_id: int | None,
    payload: dict,
    status: str = "open",
) -> ExpenseReviewQueue:
    return ExpenseReviewQueue(
        user_id=user_id,
        kind=kind,
        status=status,
        payload_json=json.dumps(payload),
        related_tx_id=related_tx_id,
        materiality=materiality,
        bucket="amount",
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def seeded_for_by_txn(client_with_db):
    SessionLocal = client_with_db.app.state.session_factory
    s: Session = SessionLocal()
    try:
        if s.get(User, "ariel") is None:
            s.add(User(id="ariel", plan="free"))
        s.commit()
        rows = [
            # tx 100 — single anomaly via related_tx_id.
            _row(
                "ariel",
                kind="a1_category_outlier",
                materiality="warning",
                related_tx_id=100,
                payload={"transaction_id": 100, "merchant_normalized": "x", "amount_nis": 500},
            ),
            # tx 200 — two anomalies.
            _row(
                "ariel",
                kind="a2_merchant_spike",
                materiality="critical",
                related_tx_id=200,
                payload={"transaction_id": 200, "merchant_normalized": "y", "amount_nis": 800},
            ),
            _row(
                "ariel",
                kind="c1_novel_merchant",
                materiality="info",
                related_tx_id=200,
                payload={"transaction_id": 200, "merchant_normalized": "y"},
            ),
            # tx 300 — resolved row, must NOT appear.
            _row(
                "ariel",
                kind="a1_category_outlier",
                materiality="critical",
                related_tx_id=300,
                payload={"transaction_id": 300, "merchant_normalized": "z"},
                status="resolved",
            ),
            # Duplicate pair: min_tx_id=400 (related), max_tx_id=401 (payload only).
            _row(
                "ariel",
                kind="d1_cross_card_duplicate",
                materiality="critical",
                related_tx_id=400,
                payload={
                    "min_tx_id": 400,
                    "max_tx_id": 401,
                    "merchant_normalized": "shufersal",
                    "amount_nis": 487,
                    "rationale": "Pair within 3d",
                },
            ),
        ]
        s.add_all(rows)
        s.commit()
    finally:
        s.close()


def test_empty_txn_ids_returns_empty_map(client_with_db, seeded_for_by_txn):
    resp = client_with_db.get("/api/anomaly/by-txn?user_id=ariel&txn_ids=")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {}


def test_direct_mapping_via_related_tx_id(client_with_db, seeded_for_by_txn):
    """tx 100 → 1 anomaly; tx 200 → 2 anomalies; tx 300 (resolved) → empty."""
    resp = client_with_db.get(
        "/api/anomaly/by-txn?user_id=ariel&txn_ids=100,200,300"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body.keys()) == {"100", "200", "300"}
    assert len(body["100"]) == 1
    assert body["100"][0]["kind"] == "large_outlier"
    assert len(body["200"]) == 2
    kinds_200 = {c["kind"] for c in body["200"]}
    assert kinds_200 == {"merchant_spike", "novel_merchant"}
    # Resolved row stays excluded.
    assert body["300"] == []


def test_duplicate_pair_max_leg_also_lights(client_with_db, seeded_for_by_txn):
    """Bucket-D: tx 401 (the LATER leg) must surface even though only
    400 is in related_tx_id. The card's txn_id is overridden to 401.
    """
    resp = client_with_db.get(
        "/api/anomaly/by-txn?user_id=ariel&txn_ids=400,401"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Both legs see the same duplicate card.
    assert len(body["400"]) == 1
    assert body["400"][0]["kind"] == "cross_card_duplicate"
    assert body["400"][0]["txn_id"] == 400
    assert len(body["401"]) == 1
    assert body["401"][0]["kind"] == "cross_card_duplicate"
    assert body["401"][0]["txn_id"] == 401
