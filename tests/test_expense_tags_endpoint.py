"""Transaction tag CRUD + tag listing + trip-summary aggregation."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from argosy.state.models import (
    ExpenseCategory, ExpenseSource, ExpenseStatement, ExpenseTransaction,
    User, UserFile,
)


def _seed(client_with_db, *, user_id: str = "u1", n: int = 3) -> list[int]:
    """Seed `n` simple debits in May 2026. Returns transaction IDs."""
    SF = client_with_db.app.state.session_factory
    ids: list[int] = []
    with SF() as s:
        s.add(User(id=user_id, plan="free")); s.flush()
        from argosy.services.expense_ingest.taxonomy_seed import (
            seed_system_defaults, seed_user_categories,
        )
        seed_system_defaults(s); s.flush()
        seed_user_categories(s, user_id); s.flush()
        f = UserFile(
            user_id=user_id, sha256="t"*64, original_name="x", sanitized_name="x",
            mime_type="x", kind="other", size_bytes=1, storage_path="/tmp/x",
            source="chat_attachment",
        )
        s.add(f); s.flush()
        src = ExpenseSource(
            user_id=user_id, kind="card", issuer="isracard",
            external_id="0001", display_name="Test",
        )
        s.add(src); s.flush()
        stmt = ExpenseStatement(
            user_id=user_id, source_id=src.id, file_id=f.id,
            period_start=date(2026, 5, 1), period_end=date(2026, 5, 31),
            parsed_total_nis=Decimal("0"),
            parser_name="isracard", parser_version="0.1.0", status="parsed",
        )
        s.add(stmt); s.flush()
        cat = s.query(ExpenseCategory).filter_by(
            user_id=user_id, slug="dining_out.restaurants",
        ).one()
        for i in range(n):
            tx = ExpenseTransaction(
                user_id=user_id, source_id=src.id, statement_id=stmt.id,
                occurred_on=date(2026, 5, i + 1),
                merchant_raw=f"M{i}", merchant_normalized=f"m{i}",
                amount_nis=Decimal("100"),
                direction="debit", tx_type="regular",
                category_id=cat.id, category_source="rule",
                category_confidence=Decimal("1.0"), raw_row_json="{}",
            )
            s.add(tx); s.flush()
            ids.append(tx.id)
        s.commit()
    return ids


def test_patch_tags_replaces_full_list(client_with_db):
    ids = _seed(client_with_db, user_id="u_tag_patch", n=1)
    tx_id = ids[0]
    r = client_with_db.patch(
        f"/api/expenses/transactions/{tx_id}/tags",
        json={"user_id": "u_tag_patch", "tags": ["trip:greece-2026-aug", "vacation"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["transaction_id"] == tx_id
    assert sorted(body["tags"]) == ["trip:greece-2026-aug", "vacation"]
    # Replace with a smaller list — old tags must be gone.
    r2 = client_with_db.patch(
        f"/api/expenses/transactions/{tx_id}/tags",
        json={"user_id": "u_tag_patch", "tags": ["business"]},
    )
    assert r2.status_code == 200
    assert r2.json()["tags"] == ["business"]


def test_add_tag_is_idempotent(client_with_db):
    ids = _seed(client_with_db, user_id="u_tag_add", n=1)
    tx_id = ids[0]
    for _ in range(3):
        r = client_with_db.post(
            f"/api/expenses/transactions/{tx_id}/tags/add",
            json={"user_id": "u_tag_add", "tag": "trip:nyc"},
        )
        assert r.status_code == 200
    assert r.json()["tags"] == ["trip:nyc"]
    # Add a different tag — list grows to 2 (sorted alphabetically).
    r = client_with_db.post(
        f"/api/expenses/transactions/{tx_id}/tags/add",
        json={"user_id": "u_tag_add", "tag": "business"},
    )
    assert r.json()["tags"] == ["business", "trip:nyc"]


def test_remove_tag_idempotent(client_with_db):
    ids = _seed(client_with_db, user_id="u_tag_rm", n=1)
    tx_id = ids[0]
    client_with_db.patch(
        f"/api/expenses/transactions/{tx_id}/tags",
        json={"user_id": "u_tag_rm", "tags": ["a", "b"]},
    )
    r = client_with_db.post(
        f"/api/expenses/transactions/{tx_id}/tags/remove",
        json={"user_id": "u_tag_rm", "tag": "a"},
    )
    assert r.json()["tags"] == ["b"]
    # Removing again is a no-op.
    r = client_with_db.post(
        f"/api/expenses/transactions/{tx_id}/tags/remove",
        json={"user_id": "u_tag_rm", "tag": "a"},
    )
    assert r.json()["tags"] == ["b"]


def test_list_tags_with_prefix(client_with_db):
    ids = _seed(client_with_db, user_id="u_tag_list", n=3)
    client_with_db.patch(
        f"/api/expenses/transactions/{ids[0]}/tags",
        json={"user_id": "u_tag_list", "tags": ["trip:greece-2026-aug", "business"]},
    )
    client_with_db.patch(
        f"/api/expenses/transactions/{ids[1]}/tags",
        json={"user_id": "u_tag_list", "tags": ["trip:nyc-2025-dec"]},
    )
    # All tags
    r = client_with_db.get("/api/expenses/tags?user_id=u_tag_list")
    assert sorted(r.json()["tags"]) == [
        "business", "trip:greece-2026-aug", "trip:nyc-2025-dec",
    ]
    # Trip-prefix only
    r = client_with_db.get("/api/expenses/tags?user_id=u_tag_list&prefix=trip:")
    assert sorted(r.json()["tags"]) == [
        "trip:greece-2026-aug", "trip:nyc-2025-dec",
    ]


def test_trip_summary_aggregates(client_with_db):
    ids = _seed(client_with_db, user_id="u_trip", n=3)
    # Tag two of the three with trip:greece — third left untagged.
    for tid in ids[:2]:
        client_with_db.post(
            f"/api/expenses/transactions/{tid}/tags/add",
            json={"user_id": "u_trip", "tag": "trip:greece-2026-aug"},
        )
    r = client_with_db.get(
        "/api/expenses/trip-summary?user_id=u_trip&tag=trip:greece-2026-aug"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["tag"] == "trip:greece-2026-aug"
    assert body["transaction_count"] == 2
    assert body["total_nis"] == pytest.approx(200.0)
    # Both txs are dining_out.restaurants (seed default).
    assert len(body["by_category"]) == 1
    assert body["by_category"][0]["slug"] == "dining_out.restaurants"
    assert body["period_start"] == "2026-05-01"
    assert body["period_end"] == "2026-05-02"
    # Currency breakdown exists for NIS.
    nis = [c for c in body["currency_breakdown"] if c["currency"] == "NIS"]
    assert nis and nis[0]["total"] == pytest.approx(200.0)


def test_trip_summary_nets_refunds_against_charges(client_with_db):
    """A book+refund pair under the same trip tag must net to zero — same
    contract as the EX8 merchant Net Total fix.

    Regression: previously trip_summary summed absolute amount_nis values,
    so a ₪2,463 booking + matching ₪2,463 refund counted as ₪4,926 instead
    of ₪0, doubling the headline trip cost. Real example surfaced from
    `vacation:romania` (Cars on Booking booked + refunded × 3).
    """
    user_id = "u_trip_refund"
    ids = _seed(client_with_db, user_id=user_id, n=2)
    # Both txs default to direction='debit' (₪100 each). Flip the second
    # to a refund of the first — same amount, same tag.
    SF = client_with_db.app.state.session_factory
    with SF() as s:
        refund_tx = s.query(ExpenseTransaction).filter_by(id=ids[1]).one()
        refund_tx.direction = "credit"
        refund_tx.tx_type = "refund"
        s.commit()
    for tid in ids:
        client_with_db.post(
            f"/api/expenses/transactions/{tid}/tags/add",
            json={"user_id": user_id, "tag": "trip:cancun"},
        )
    r = client_with_db.get(
        f"/api/expenses/trip-summary?user_id={user_id}&tag=trip:cancun"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["transaction_count"] == 2  # both rows still listed
    assert body["total_nis"] == pytest.approx(0.0)  # but net cost is zero
    # Per-category total also nets to zero.
    cats = body["by_category"]
    assert len(cats) == 1
    assert cats[0]["total_nis"] == pytest.approx(0.0)
    # Currency breakdown for NIS nets to zero too.
    nis = [c for c in body["currency_breakdown"] if c["currency"] == "NIS"]
    assert nis and nis[0]["total"] == pytest.approx(0.0)


def test_trip_summary_converts_foreign_currency_into_total_nis(client_with_db):
    """A USD-native row with no amount_nis must still contribute to total_nis
    via fx.convert. Regression: before the conversion was wired, foreign-only
    rows had amount_nis=None and dropped out of the headline, so a USD-only
    flight on a trip showed `total_nis = 0` even though the trip clearly cost
    money. Real example: vacation:romania (Anima Wings $1,233.80 flight).
    """
    user_id = "u_trip_fx"
    ids = _seed(client_with_db, user_id=user_id, n=1)
    # Seed an FX rate (ILS per USD) for the seed-default date 2026-05-01.
    SF = client_with_db.app.state.session_factory
    with SF() as s:
        from argosy.state.models import FxRate
        s.add(FxRate(
            currency="USD", date=date(2026, 5, 1), rate=Decimal("3.70"),
            source="test", fetched_at=datetime(2026, 5, 1),
        ))
        # Flip the only seeded row from NIS-native to USD-native.
        tx = s.query(ExpenseTransaction).filter_by(id=ids[0]).one()
        tx.amount_nis = None
        tx.currency_orig = "USD"
        tx.amount_orig = Decimal("100")  # $100 -> ₪370 at 3.70
        s.commit()
    client_with_db.post(
        f"/api/expenses/transactions/{ids[0]}/tags/add",
        json={"user_id": user_id, "tag": "trip:tokyo"},
    )
    r = client_with_db.get(
        f"/api/expenses/trip-summary?user_id={user_id}&tag=trip:tokyo"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["transaction_count"] == 1
    assert body["total_nis"] == pytest.approx(370.0, rel=1e-3)
    # Currency breakdown reports the foreign currency natively.
    usd = [c for c in body["currency_breakdown"] if c["currency"] == "USD"]
    assert usd and usd[0]["total"] == pytest.approx(100.0)


def test_transactions_filter_by_tag(client_with_db):
    ids = _seed(client_with_db, user_id="u_tag_filter", n=3)
    client_with_db.post(
        f"/api/expenses/transactions/{ids[0]}/tags/add",
        json={"user_id": "u_tag_filter", "tag": "trip:tokyo"},
    )
    r = client_with_db.get(
        "/api/expenses/transactions?user_id=u_tag_filter&tag=trip:tokyo"
    )
    assert r.status_code == 200
    txs = r.json()["transactions"]
    assert len(txs) == 1
    assert ids[0] == txs[0]["id"]
    # Untagged tx is NOT returned.
    assert ids[1] != txs[0]["id"]


def test_patch_unknown_transaction_returns_404(client_with_db):
    r = client_with_db.patch(
        "/api/expenses/transactions/99999/tags",
        json={"user_id": "u_anyone", "tags": ["x"]},
    )
    assert r.status_code == 404


def test_transaction_out_includes_tags_field(client_with_db):
    """Existing TransactionOut must now include `tags: list[str]` so the UI
    can render chips inline. Defaults to [] when no tag has been set.
    """
    ids = _seed(client_with_db, user_id="u_tx_field", n=1)
    client_with_db.post(
        f"/api/expenses/transactions/{ids[0]}/tags/add",
        json={"user_id": "u_tx_field", "tag": "alpha"},
    )
    r = client_with_db.get("/api/expenses/transactions?user_id=u_tx_field")
    txs = r.json()["transactions"]
    assert len(txs) == 1
    assert txs[0]["tags"] == ["alpha"]
