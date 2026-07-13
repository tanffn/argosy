"""Expense tag rules — retroactive apply, exact-match, ingest hook, bulk-add."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from argosy.services.expense_tag_rules import (
    apply_tag_rules,
    bulk_add_tag,
    create_tag_rule,
    parse_tags,
)
from argosy.state.models import (
    ExpenseCategory,
    ExpenseSource,
    ExpenseStatement,
    ExpenseTagRule,
    ExpenseTransaction,
    User,
    UserFile,
)

PAZ_YELLOW = "פז אפליקציית יילו"
GROCERY = "פזית מרקט 24"
INSURANCE = "איילון ביטוח כללי"


def _seed_user(session, user_id: str = "u_tag_rules"):
    session.add(User(id=user_id, plan="free"))
    session.flush()
    from argosy.services.expense_ingest.taxonomy_seed import (
        seed_system_defaults,
        seed_user_categories,
    )
    seed_system_defaults(session)
    session.flush()
    seed_user_categories(session, user_id)
    session.flush()
    f = UserFile(
        user_id=user_id, sha256="r" * 64, original_name="x", sanitized_name="x",
        mime_type="x", kind="other", size_bytes=1, storage_path="/tmp/x",
        source="chat_attachment",
    )
    session.add(f)
    session.flush()
    src = ExpenseSource(
        user_id=user_id, kind="card", issuer="isracard",
        external_id="9999", display_name="Test",
    )
    session.add(src)
    session.flush()
    stmt = ExpenseStatement(
        user_id=user_id, source_id=src.id, file_id=f.id,
        period_start=date(2026, 5, 1), period_end=date(2026, 5, 31),
        parsed_total_nis=Decimal("0"),
        parser_name="isracard", parser_version="0.1.0", status="parsed",
    )
    session.add(stmt)
    session.flush()
    fuel = session.query(ExpenseCategory).filter_by(
        user_id=user_id, slug="transport.fuel",
    ).one_or_none()
    if fuel is None:
        # Fall back to any category if fuel slug isn't seeded.
        fuel = session.query(ExpenseCategory).filter_by(user_id=user_id).first()
    groceries = session.query(ExpenseCategory).filter_by(
        user_id=user_id, slug="groceries.supermarket",
    ).one_or_none() or fuel
    return user_id, src, stmt, fuel, groceries


def _tx(session, *, user_id, src, stmt, merchant, cat, day: int, tags="[]"):
    row = ExpenseTransaction(
        user_id=user_id, source_id=src.id, statement_id=stmt.id,
        occurred_on=date(2026, 5, day),
        merchant_raw=merchant, merchant_normalized=merchant,
        amount_nis=Decimal("50"),
        direction="debit", tx_type="regular",
        category_id=cat.id if cat is not None else None,
        category_source="rule",
        category_confidence=Decimal("1.0"),
        raw_row_json="{}",
        tags=tags,
    )
    session.add(row)
    session.flush()
    return row


def test_create_rule_retroactively_tags_exact_merchant(client_with_db):
    SF = client_with_db.app.state.session_factory
    with SF() as s:
        user_id, src, stmt, fuel, groceries = _seed_user(s, "u_paz")
        for i in range(1, 4):
            _tx(s, user_id=user_id, src=src, stmt=stmt,
                merchant=PAZ_YELLOW, cat=fuel, day=i)
        _tx(s, user_id=user_id, src=src, stmt=stmt,
            merchant=GROCERY, cat=groceries, day=10)
        _tx(s, user_id=user_id, src=src, stmt=stmt,
            merchant=INSURANCE, cat=fuel, day=11)
        s.commit()

    r = client_with_db.post(
        "/api/expenses/tag-rules",
        json={
            "user_id": "u_paz",
            "match_merchant_normalized": PAZ_YELLOW,
            "tag": "Mazda",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tagged_count"] == 3
    assert body["rule"]["tag"] == "Mazda"
    assert body["rule"]["match_merchant_normalized"] == PAZ_YELLOW

    with SF() as s:
        rows = s.query(ExpenseTransaction).filter_by(user_id="u_paz").all()
        paz = [t for t in rows if t.merchant_normalized == PAZ_YELLOW]
        assert len(paz) == 3
        assert all("Mazda" in parse_tags(t.tags) for t in paz)
        assert all(
            "Mazda" not in parse_tags(t.tags)
            for t in rows if t.merchant_normalized != PAZ_YELLOW
        )


def test_exact_match_does_not_tag_substring_merchants(client_with_db):
    SF = client_with_db.app.state.session_factory
    with SF() as s:
        user_id, src, stmt, fuel, groceries = _seed_user(s, "u_exact")
        _tx(s, user_id=user_id, src=src, stmt=stmt,
            merchant=PAZ_YELLOW, cat=fuel, day=1)
        _tx(s, user_id=user_id, src=src, stmt=stmt,
            merchant=GROCERY, cat=groceries, day=2)
        _tx(s, user_id=user_id, src=src, stmt=stmt,
            merchant=INSURANCE, cat=fuel, day=3)
        create_tag_rule(
            s, user_id,
            match_merchant_normalized=PAZ_YELLOW,
            tag="Mazda",
        )
        s.commit()

    with SF() as s:
        rows = {
            t.merchant_normalized: parse_tags(t.tags)
            for t in s.query(ExpenseTransaction).filter_by(user_id="u_exact")
        }
        assert "Mazda" in rows[PAZ_YELLOW]
        assert "Mazda" not in rows[GROCERY]
        assert "Mazda" not in rows[INSURANCE]


def test_apply_tag_rules_on_new_tx_ids(client_with_db):
    """Simulate ingest: new tx lands, apply_tag_rules tags it."""
    SF = client_with_db.app.state.session_factory
    with SF() as s:
        user_id, src, stmt, fuel, _ = _seed_user(s, "u_ingest")
        create_tag_rule(
            s, user_id,
            match_merchant_normalized=PAZ_YELLOW,
            tag="Mazda",
            apply_retroactive=False,
        )
        new_tx = _tx(
            s, user_id=user_id, src=src, stmt=stmt,
            merchant=PAZ_YELLOW, cat=fuel, day=20,
        )
        n = apply_tag_rules(s, user_id, tx_ids=[new_tx.id])
        s.commit()
        assert n == 1
        s.refresh(new_tx)
        assert "Mazda" in parse_tags(new_tx.tags)


def test_bulk_add_by_ids_and_by_filter(client_with_db):
    SF = client_with_db.app.state.session_factory
    with SF() as s:
        user_id, src, stmt, fuel, _ = _seed_user(s, "u_bulk")
        a = _tx(s, user_id=user_id, src=src, stmt=stmt,
                merchant="alpha", cat=fuel, day=1)
        b = _tx(s, user_id=user_id, src=src, stmt=stmt,
                merchant="beta", cat=fuel, day=2)
        c = _tx(s, user_id=user_id, src=src, stmt=stmt,
                merchant="alpha", cat=fuel, day=3)
        s.commit()
        a_id, b_id, c_id = a.id, b.id, c.id

    r = client_with_db.post(
        "/api/expenses/transactions/tags/bulk-add",
        json={
            "user_id": "u_bulk",
            "tag": "picked",
            "transaction_ids": [a_id, b_id],
        },
    )
    assert r.status_code == 200
    assert r.json()["tagged_count"] == 2

    r2 = client_with_db.post(
        "/api/expenses/transactions/tags/bulk-add",
        json={
            "user_id": "u_bulk",
            "tag": "alpha-only",
            "merchant_normalized": "alpha",
        },
    )
    assert r2.status_code == 200
    assert r2.json()["tagged_count"] == 2  # a + c

    with SF() as s:
        rows = {t.id: parse_tags(t.tags) for t in s.query(ExpenseTransaction).filter_by(user_id="u_bulk")}
        assert "picked" in rows[a_id] and "picked" in rows[b_id]
        assert "alpha-only" in rows[a_id] and "alpha-only" in rows[c_id]
        assert "alpha-only" not in rows[b_id]


def test_delete_rule_leaves_tags(client_with_db):
    SF = client_with_db.app.state.session_factory
    with SF() as s:
        user_id, src, stmt, fuel, _ = _seed_user(s, "u_del")
        _tx(s, user_id=user_id, src=src, stmt=stmt,
            merchant=PAZ_YELLOW, cat=fuel, day=1)
        rule, n = create_tag_rule(
            s, user_id,
            match_merchant_normalized=PAZ_YELLOW,
            tag="Mazda",
        )
        s.commit()
        rule_id = rule.id
        assert n == 1

    r = client_with_db.delete(
        f"/api/expenses/tag-rules/{rule_id}?user_id=u_del",
    )
    assert r.status_code == 200

    with SF() as s:
        assert s.query(ExpenseTagRule).filter_by(id=rule_id).one_or_none() is None
        tx = s.query(ExpenseTransaction).filter_by(user_id="u_del").one()
        assert "Mazda" in parse_tags(tx.tags)


def test_list_tag_rules(client_with_db):
    SF = client_with_db.app.state.session_factory
    with SF() as s:
        user_id, *_ = _seed_user(s, "u_list")
        create_tag_rule(
            s, user_id,
            match_merchant_normalized=PAZ_YELLOW,
            tag="Mazda",
            apply_retroactive=False,
        )
        s.commit()

    r = client_with_db.get("/api/expenses/tag-rules?user_id=u_list")
    assert r.status_code == 200
    rules = r.json()["rules"]
    assert len(rules) == 1
    assert rules[0]["tag"] == "Mazda"
