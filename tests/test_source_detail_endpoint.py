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
