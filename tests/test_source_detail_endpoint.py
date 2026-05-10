"""Source-detail endpoint — per-source statement timeline + reconciliation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from argosy.state.models import (
    ExpenseSource, ExpenseStatement, ExpenseTransaction, User, UserFile,
)


def _seed(client_with_db, user_id="u1", source_id_out=None):
    SF = client_with_db.app.state.session_factory
    ids = {}
    with SF() as s:
        s.add(User(id=user_id, plan="free")); s.flush()
        f = UserFile(
            user_id=user_id, sha256="b"*64, original_name="x", sanitized_name="x",
            mime_type="x", kind="other", size_bytes=1, storage_path="/tmp/x",
            source="chat_attachment",
        )
        s.add(f); s.flush()
        src = ExpenseSource(
            user_id=user_id, kind="card", issuer="isracard",
            external_id="0000", display_name="Test Card",
        )
        s.add(src); s.flush()
        ids["source_id"] = src.id
        # Two statements
        for month in (3, 4):
            stmt = ExpenseStatement(
                user_id=user_id, source_id=src.id, file_id=f.id,
                period_start=date(2026, month, 1),
                period_end=date(2026, month, 28),
                parsed_total_nis=Decimal("100"),
                declared_total_nis=Decimal("100"),
                parser_name="isracard", parser_version="0.1.0", status="parsed",
            )
            s.add(stmt); s.flush()
            for i in range(3):
                s.add(ExpenseTransaction(
                    user_id=user_id, source_id=src.id, statement_id=stmt.id,
                    occurred_on=date(2026, month, i + 5),
                    merchant_raw=f"M{month}-{i}",
                    merchant_normalized=f"m{month}{i}",
                    amount_nis=Decimal("33"), direction="debit", tx_type="regular",
                    raw_row_json="{}",
                ))
        s.commit()
    return ids


def test_source_detail_returns_timeline(client_with_db):
    ids = _seed(client_with_db)
    sid = ids["source_id"]
    r = client_with_db.get(f"/api/expenses/source-detail/{sid}?user_id=u1")
    assert r.status_code == 200
    body = r.json()
    assert body["source"]["id"] == sid
    assert body["source"]["display_name"] == "Test Card"
    assert len(body["statements"]) == 2
    # Sorted oldest-first
    assert body["statements"][0]["period_start"] < body["statements"][1]["period_start"]
    for s in body["statements"]:
        assert s["transaction_count"] == 3


def test_source_detail_unknown_source_returns_404(client_with_db):
    r = client_with_db.get("/api/expenses/source-detail/99999?user_id=u_anyone")
    assert r.status_code == 404


def test_source_detail_includes_monthly_buckets(client_with_db):
    """months[] is derived from tx.occurred_on, NOT statement period.
    With 2 statements x 3 txs each across distinct months we get 2 buckets.
    Important: this works even when statements span an entire year (Discount).
    """
    ids = _seed(client_with_db, user_id="u_months")
    sid = ids["source_id"]
    r = client_with_db.get(f"/api/expenses/source-detail/{sid}?user_id=u_months")
    assert r.status_code == 200
    body = r.json()
    assert "months" in body
    months = body["months"]
    assert len(months) == 2
    # Sorted ascending YYYY-MM
    assert months[0]["month"] == "2026-03"
    assert months[1]["month"] == "2026-04"
    for mb in months:
        assert mb["debit_nis"] == pytest.approx(99.0)  # 3 * 33
        assert mb["credit_nis"] == 0.0
        assert mb["transaction_count"] == 3
        assert mb["correlated_count"] == 0


def test_source_detail_monthly_buckets_split_one_statement(client_with_db):
    """One Discount-like big statement spanning Jan→Mar still produces 3
    monthly buckets — that's the user's real complaint about the timeline.
    """
    SF = client_with_db.app.state.session_factory
    user_id = "u_discount"
    with SF() as s:
        s.add(User(id=user_id, plan="free")); s.flush()
        f = UserFile(
            user_id=user_id, sha256="d"*64, original_name="x", sanitized_name="x",
            mime_type="x", kind="other", size_bytes=1, storage_path="/tmp/x",
            source="chat_attachment",
        )
        s.add(f); s.flush()
        src = ExpenseSource(
            user_id=user_id, kind="card", issuer="discount",
            external_id="2923", display_name="Discount 2923",
        )
        s.add(src); s.flush()
        sid = src.id
        # ONE statement spanning Jan→Mar 2026 (the Discount export style).
        stmt = ExpenseStatement(
            user_id=user_id, source_id=src.id, file_id=f.id,
            period_start=date(2026, 1, 1), period_end=date(2026, 3, 31),
            parsed_total_nis=Decimal("300"),
            declared_total_nis=Decimal("300"),
            parser_name="discount", parser_version="0.1.0", status="parsed",
        )
        s.add(stmt); s.flush()
        for month in (1, 2, 3):
            s.add(ExpenseTransaction(
                user_id=user_id, source_id=src.id, statement_id=stmt.id,
                occurred_on=date(2026, month, 15),
                merchant_raw=f"M{month}", merchant_normalized=f"m{month}",
                amount_nis=Decimal("100"), direction="debit", tx_type="regular",
                raw_row_json="{}",
            ))
        s.commit()
    r = client_with_db.get(f"/api/expenses/source-detail/{sid}?user_id={user_id}")
    body = r.json()
    # Just one statement…
    assert len(body["statements"]) == 1
    # …but THREE monthly buckets — solves the user's "Discount shows 2 bars" issue.
    assert len(body["months"]) == 3
    assert [m["month"] for m in body["months"]] == ["2026-01", "2026-02", "2026-03"]
