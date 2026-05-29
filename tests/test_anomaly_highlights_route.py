"""Sprint #2 commit #10 — GET /api/anomaly/highlights tests.

Covers:
  * Empty user → 200 + ``[]``.
  * Severity ordering: critical > warning > info; ties break on
    created_at DESC.
  * limit clamps the result set + drops unmappable kinds.
  * Detector-kind → AnomalyCard-kind mapping (one assertion per spec
    mapping).
  * ``link`` and ``txn_id`` propagate from related_tx_id / payload.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from argosy.state.models import ExpenseReviewQueue, User


def _make_row(
    user_id: str,
    *,
    kind: str,
    materiality: str | None,
    payload: dict,
    related_tx_id: int | None = None,
    created_at: datetime | None = None,
    status: str = "open",
    bucket: str | None = None,
    dedup_key: str | None = None,
) -> ExpenseReviewQueue:
    return ExpenseReviewQueue(
        user_id=user_id,
        kind=kind,
        status=status,
        payload_json=json.dumps(payload),
        related_tx_id=related_tx_id,
        materiality=materiality,
        bucket=bucket,
        dedup_key=dedup_key,
        created_at=created_at or datetime.now(timezone.utc),
    )


@pytest.fixture
def seeded_review_queue(client_with_db):
    """Seven open queue rows for 'ariel' covering every detector kind
    plus one 'resolved' row that the route MUST skip.
    """
    SessionLocal = client_with_db.app.state.session_factory
    s: Session = SessionLocal()
    try:
        if s.get(User, "ariel") is None:
            s.add(User(id="ariel", plan="free"))
        if s.get(User, "intruder") is None:
            s.add(User(id="intruder", plan="free"))
        s.commit()

        base = datetime(2026, 5, 27, 8, 0, tzinfo=timezone.utc)
        rows = [
            # critical — should sort first.
            _make_row(
                "ariel",
                kind="d1_cross_card_duplicate",
                materiality="critical",
                payload={
                    "detector": "d1_cross_card_duplicate",
                    "rationale": "Same merchant + amount within 3d",
                    "min_tx_id": 100,
                    "max_tx_id": 101,
                    "merchant_normalized": "shufersal",
                    "amount_nis": 487.30,
                },
                related_tx_id=100,
                bucket="duplicate",
                created_at=base + timedelta(hours=1),
            ),
            # warning — a1.
            _make_row(
                "ariel",
                kind="a1_category_outlier",
                materiality="warning",
                payload={
                    "detector": "a1_category_outlier",
                    "rationale": "5.2σ above category median",
                    "transaction_id": 200,
                    "merchant_normalized": "decathlon",
                    "amount_nis": 1230.0,
                },
                related_tx_id=200,
                bucket="amount",
                created_at=base + timedelta(hours=2),
            ),
            # warning — a2.
            _make_row(
                "ariel",
                kind="a2_merchant_spike",
                materiality="warning",
                payload={
                    "detector": "a2_merchant_spike",
                    "rationale": "4.1x trailing mean",
                    "transaction_id": 201,
                    "merchant_normalized": "wolt",
                    "amount_nis": 220.0,
                },
                related_tx_id=201,
                bucket="amount",
                created_at=base + timedelta(hours=3),
            ),
            # info — c1 novel merchant.
            _make_row(
                "ariel",
                kind="c1_novel_merchant",
                materiality="info",
                payload={
                    "detector": "c1_novel_merchant",
                    "rationale": "Never seen before",
                    "transaction_id": 300,
                    "merchant_normalized": "new shop",
                },
                related_tx_id=300,
                bucket="cache",
                created_at=base + timedelta(hours=4),
            ),
            # info — c2 drift.
            _make_row(
                "ariel",
                kind="c2_category_drift",
                materiality="info",
                payload={
                    "detector": "c2_category_drift",
                    "rationale": "Was groceries, now dining",
                    "transaction_id": 301,
                    "merchant_normalized": "ampm",
                },
                related_tx_id=301,
                bucket="cache",
                created_at=base + timedelta(hours=5),
            ),
            # warning — bucket_b recurring missing (no related_tx_id).
            _make_row(
                "ariel",
                kind="bucket_b_recurring_missing",
                materiality="warning",
                payload={
                    "pattern_id": 7,
                    "merchant_normalized": "spotify",
                    "expected_amount_nis": "21.90",
                    "expected_on": "2026-05-15",
                    "last_seen": "2026-04-15",
                    "cadence_days": 30,
                    "grace_days": 7,
                    "days_overdue": 12,
                },
                related_tx_id=None,
                bucket="recurring",
                created_at=base + timedelta(hours=6),
            ),
            # critical — fee waiver missing.
            _make_row(
                "ariel",
                kind="bucket_b_fee_waiver_missing",
                materiality="critical",
                payload={
                    "watchlist_entry_id": 1,
                    "observation_period": "2026-05-01",
                },
                related_tx_id=400,
                bucket="recurring",
                created_at=base + timedelta(hours=7),
            ),
            # RESOLVED — must be excluded.
            _make_row(
                "ariel",
                kind="a1_category_outlier",
                materiality="critical",
                payload={"detector": "a1_category_outlier"},
                related_tx_id=999,
                status="resolved",
                created_at=base + timedelta(hours=8),
            ),
            # intruder row — must be excluded by user_id filter.
            _make_row(
                "intruder",
                kind="a1_category_outlier",
                materiality="critical",
                payload={"detector": "a1_category_outlier"},
                related_tx_id=998,
                created_at=base + timedelta(hours=9),
            ),
        ]
        s.add_all(rows)
        s.commit()
    finally:
        s.close()


def test_empty_user_returns_empty_list(client_with_db):
    """No queue rows → 200 + ``[]`` (NOT 204, NOT 404)."""
    SessionLocal = client_with_db.app.state.session_factory
    with SessionLocal() as s:
        if s.get(User, "ariel") is None:
            s.add(User(id="ariel", plan="free"))
            s.commit()

    resp = client_with_db.get("/api/anomaly/highlights?user_id=ariel")
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


def test_severity_ordering_critical_first(client_with_db, seeded_review_queue):
    """Critical rows sort before warnings; warnings before info."""
    resp = client_with_db.get("/api/anomaly/highlights?user_id=ariel&limit=10")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # We seeded 7 open rows for 'ariel'.
    assert len(body) == 7
    sev_order = [c["severity"] for c in body]
    # Both criticals first, then four warnings, then both infos.
    assert sev_order[0] == "critical"
    assert sev_order[1] == "critical"
    # Last two infos.
    assert sev_order[-1] == "info"
    assert sev_order[-2] == "info"
    # No "open=resolved" leakage.
    assert all(c["id"] > 0 for c in body)


def test_limit_clamps_results(client_with_db, seeded_review_queue):
    """``limit=3`` returns exactly 3 cards, the top-3 by severity."""
    resp = client_with_db.get("/api/anomaly/highlights?user_id=ariel&limit=3")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 3
    # Two criticals + the most-recent warning.
    assert body[0]["severity"] == "critical"
    assert body[1]["severity"] == "critical"
    assert body[2]["severity"] == "warning"


def test_kind_mapping(client_with_db, seeded_review_queue):
    """Every detector kind maps to the spec'd AnomalyCard kind."""
    resp = client_with_db.get("/api/anomaly/highlights?user_id=ariel&limit=20")
    body = resp.json()
    kinds = {c["kind"] for c in body}
    # Per spec §2.2 mapping:
    assert "cross_card_duplicate" in kinds  # d1
    assert "large_outlier" in kinds          # a1
    assert "merchant_spike" in kinds         # a2
    assert "novel_merchant" in kinds         # c1
    assert "category_drift" in kinds         # c2
    assert "recurring_missing" in kinds      # bucket_b_recurring_missing
    assert "fee_waiver_missed" in kinds      # bucket_b_fee_waiver_missing


def test_link_and_txn_id_propagate(client_with_db, seeded_review_queue):
    """txn-anchored rows expose ``txn_id`` and a deep-link."""
    resp = client_with_db.get("/api/anomaly/highlights?user_id=ariel&limit=20")
    body = resp.json()
    by_kind = {c["kind"]: c for c in body}

    # a1 row → txn_id 200 from related_tx_id.
    large = by_kind["large_outlier"]
    assert large["txn_id"] == 200
    assert large["link"] == "/expenses/transactions?highlight_tx=200"

    # bucket_b_recurring_missing has no related_tx_id → no link, no txn_id.
    rec = by_kind["recurring_missing"]
    assert rec["txn_id"] is None
    assert rec["link"] is None
    # Detail uses the expected_on/last_seen fallback.
    assert rec["detail"] is not None
    assert "2026-05-15" in rec["detail"]

    # d1 anchors on the EARLIER leg (min_tx_id=100).
    dup = by_kind["cross_card_duplicate"]
    assert dup["txn_id"] == 100
    assert dup["link"] == "/expenses/transactions?highlight_tx=100"
