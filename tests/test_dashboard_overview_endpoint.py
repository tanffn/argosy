"""Dashboard-overview endpoint — returns one bundle for the overview page."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from argosy.state.models import (
    ExpenseCategory, ExpenseSource, ExpenseStatement, ExpenseTransaction,
    User, UserFile,
)


def _seed_minimal(client_with_db, *, user_id: str = "u1"):
    SF = client_with_db.app.state.session_factory
    with SF() as s:
        s.add(User(id=user_id, plan="free")); s.flush()
        # Seed system + user categories
        from argosy.services.expense_ingest.taxonomy_seed import (
            seed_system_defaults, seed_user_categories,
        )
        seed_system_defaults(s); s.flush()
        seed_user_categories(s, user_id); s.flush()
        # Source + statement + 5 transactions in current month
        f = UserFile(
            user_id=user_id, sha256="a"*64, original_name="x", sanitized_name="x",
            mime_type="x", kind="other", size_bytes=1, storage_path="/tmp/x",
            source="chat_attachment",
        )
        s.add(f); s.flush()
        src = ExpenseSource(
            user_id=user_id, kind="card", issuer="isracard",
            external_id="0000", display_name="test",
        )
        s.add(src); s.flush()
        stmt = ExpenseStatement(
            user_id=user_id, source_id=src.id, file_id=f.id,
            period_start=date(2026, 5, 1), period_end=date(2026, 5, 31),
            parsed_total_nis=Decimal("250"), declared_total_nis=Decimal("250"),
            parser_name="isracard", parser_version="0.1.0", status="parsed",
        )
        s.add(stmt); s.flush()
        cat = s.query(ExpenseCategory).filter_by(
            user_id=user_id, slug="dining_out.restaurants",
        ).one()
        for i in range(5):
            s.add(ExpenseTransaction(
                user_id=user_id, source_id=src.id, statement_id=stmt.id,
                occurred_on=date(2026, 5, i + 1),
                merchant_raw=f"M{i}", merchant_normalized=f"m{i}",
                amount_nis=Decimal("50"), direction="debit", tx_type="regular",
                category_id=cat.id, category_source="user",
                category_confidence=Decimal("1.0"),
                raw_row_json="{}",
            ))
        s.commit()


def test_dashboard_overview_returns_full_payload(client_with_db):
    _seed_minimal(client_with_db)
    r = client_with_db.get("/api/expenses/dashboard-overview?user_id=u1&months=6")
    assert r.status_code == 200
    body = r.json()
    assert "months" in body
    assert "current_month_top_categories" in body
    assert "top_merchants_current_month" in body
    assert "anomalies" in body
    assert "sources_health" in body
    assert "fx_mode" in body


def test_dashboard_overview_empty_corpus_returns_empty_lists(client_with_db):
    r = client_with_db.get("/api/expenses/dashboard-overview?user_id=u_empty&months=6")
    assert r.status_code == 200
    body = r.json()
    assert body["months"] == []
    assert body["sources_health"] == []
    assert body["anomalies"] == []


def test_dashboard_overview_top_categories_ordered_by_spend(client_with_db):
    _seed_minimal(client_with_db, user_id="u2")
    r = client_with_db.get("/api/expenses/dashboard-overview?user_id=u2&months=6")
    body = r.json()
    cats = body["current_month_top_categories"]
    # Each entry: {slug, label_en, total_nis, percent}
    assert all("slug" in c and "total_nis" in c for c in cats)
    if len(cats) >= 2:
        assert cats[0]["total_nis"] >= cats[1]["total_nis"]


def test_dashboard_overview_sources_health_includes_status(client_with_db):
    _seed_minimal(client_with_db, user_id="u3")
    r = client_with_db.get("/api/expenses/dashboard-overview?user_id=u3&months=6")
    body = r.json()
    assert len(body["sources_health"]) == 1
    src = body["sources_health"][0]
    assert src["display_name"] == "test"
    # gap < 0.5 → green
    assert src["status"] == "green"
    assert src["gap"] is not None and abs(src["gap"]) < 0.5


def test_dashboard_overview_yearly_summary_shape(client_with_db):
    """yearly_summary is the 12-month rollup that powers the 'Bottom line' card.

    Asserts shape + sane values for a single-month seed: 5 NIS-50 dining-out
    transactions in May 2026 → total_nis=250, avg_per_month_nis=250 (only
    1 month in the window), current_vs_avg_pct=0.0 (current == avg), and
    top_categories_12m holds a single dining_out.restaurants entry.
    """
    _seed_minimal(client_with_db, user_id="u_year")
    r = client_with_db.get("/api/expenses/dashboard-overview?user_id=u_year&months=12")
    assert r.status_code == 200
    body = r.json()
    assert "yearly_summary" in body
    ys = body["yearly_summary"]
    assert set(ys.keys()) == {
        "months_covered", "total_nis",
        "yearly_spending_total_nis", "yearly_inflow_total_nis",
        "avg_per_month_nis",
        "top_categories_12m", "current_vs_avg_pct",
    }
    assert ys["months_covered"] == 1
    # total_nis is the (deprecated) alias for yearly_spending_total_nis.
    assert ys["total_nis"] == pytest.approx(250.0)
    assert ys["yearly_spending_total_nis"] == pytest.approx(250.0)
    assert ys["yearly_inflow_total_nis"] == pytest.approx(0.0)
    assert ys["avg_per_month_nis"] == pytest.approx(250.0)
    # cur month equals the only month present → ratio is 1.0 → pct == 0
    assert ys["current_vs_avg_pct"] == pytest.approx(0.0)
    # top categories: list of CategorySpend, sorted desc, top one is dining
    assert isinstance(ys["top_categories_12m"], list)
    assert len(ys["top_categories_12m"]) >= 1
    top = ys["top_categories_12m"][0]
    assert {"slug", "label_en", "total_nis", "transaction_count", "percent"} <= set(top.keys())
    assert top["slug"] == "dining_out.restaurants"
    assert top["total_nis"] == pytest.approx(250.0)


def test_dashboard_overview_yearly_summary_empty_corpus(client_with_db):
    """Empty corpus should not blow up — yearly_summary returns zeros + null."""
    r = client_with_db.get("/api/expenses/dashboard-overview?user_id=u_void&months=12")
    assert r.status_code == 200
    body = r.json()
    ys = body["yearly_summary"]
    assert ys["months_covered"] == 0
    assert ys["total_nis"] == 0.0
    assert ys["yearly_spending_total_nis"] == 0.0
    assert ys["yearly_inflow_total_nis"] == 0.0
    assert ys["avg_per_month_nis"] == 0.0
    assert ys["top_categories_12m"] == []
    assert ys["current_vs_avg_pct"] is None
    # Hero scalars are zero too.
    assert body["current_month"] is None
    assert body["current_month_spending_nis"] == 0.0
    assert body["current_month_inflow_nis"] == 0.0
    assert body["current_month_inflow"] == []


# ---------------------------------------------------------------------------
# Fix A — inflow vs spending separation
# ---------------------------------------------------------------------------

def _seed_inflow_and_spend(client_with_db, *, user_id: str = "u_mix",
                            month: tuple[int, int] = (2026, 5)):
    """Seed: 1 Salary credit (10000) + 1 Dining debit (200) in given month."""
    SF = client_with_db.app.state.session_factory
    with SF() as s:
        s.add(User(id=user_id, plan="free")); s.flush()
        from argosy.services.expense_ingest.taxonomy_seed import (
            seed_system_defaults, seed_user_categories,
        )
        seed_system_defaults(s); s.flush()
        seed_user_categories(s, user_id); s.flush()
        f = UserFile(
            user_id=user_id, sha256="b"*64, original_name="x", sanitized_name="x",
            mime_type="x", kind="other", size_bytes=1, storage_path="/tmp/x",
            source="chat_attachment",
        )
        s.add(f); s.flush()
        src = ExpenseSource(
            user_id=user_id, kind="bank_account", issuer="leumi",
            external_id="0001", display_name="Leumi checking",
        )
        s.add(src); s.flush()
        y, m = month
        from calendar import monthrange
        last_day = monthrange(y, m)[1]
        stmt = ExpenseStatement(
            user_id=user_id, source_id=src.id, file_id=f.id,
            period_start=date(y, m, 1), period_end=date(y, m, last_day),
            parsed_total_nis=Decimal("10200"),
            parser_name="leumi", parser_version="0.1.0", status="parsed",
        )
        s.add(stmt); s.flush()
        salary_cat = s.query(ExpenseCategory).filter_by(
            user_id=user_id, slug="income.salary",
        ).one()
        dining_cat = s.query(ExpenseCategory).filter_by(
            user_id=user_id, slug="dining_out.restaurants",
        ).one()
        s.add(ExpenseTransaction(
            user_id=user_id, source_id=src.id, statement_id=stmt.id,
            occurred_on=date(y, m, 1),
            merchant_raw="SALARY DEPOSIT", merchant_normalized="salary deposit",
            amount_nis=Decimal("10000"),
            direction="credit", tx_type="regular",
            category_id=salary_cat.id, category_source="rule",
            category_confidence=Decimal("1.0"), raw_row_json="{}",
        ))
        s.add(ExpenseTransaction(
            user_id=user_id, source_id=src.id, statement_id=stmt.id,
            occurred_on=date(y, m, 5),
            merchant_raw="RESTAURANT XYZ", merchant_normalized="restaurant xyz",
            amount_nis=Decimal("200"),
            direction="debit", tx_type="regular",
            category_id=dining_cat.id, category_source="rule",
            category_confidence=Decimal("1.0"), raw_row_json="{}",
        ))
        s.commit()


def test_dashboard_overview_separates_inflow_from_spending(client_with_db):
    """SALARY (inflow) must NOT appear in current_month_top_categories;
    Dining (debit) must NOT appear in current_month_inflow.
    This is the bug the user surfaced: salary leaking into 'top spending'.
    """
    _seed_inflow_and_spend(client_with_db, user_id="u_sep")
    r = client_with_db.get(
        "/api/expenses/dashboard-overview?user_id=u_sep&months=6"
    )
    assert r.status_code == 200
    body = r.json()
    spend_slugs = [c["slug"] for c in body["current_month_top_categories"]]
    inflow_slugs = [c["slug"] for c in body["current_month_inflow"]]
    assert "dining_out.restaurants" in spend_slugs
    assert "income.salary" not in spend_slugs
    assert "income.salary" in inflow_slugs
    assert "dining_out.restaurants" not in inflow_slugs
    # Hero scalars match the split:
    assert body["current_month_spending_nis"] == pytest.approx(200.0)
    assert body["current_month_inflow_nis"] == pytest.approx(10000.0)
    # Yearly summary mirrors the split too.
    ys = body["yearly_summary"]
    assert ys["yearly_spending_total_nis"] == pytest.approx(200.0)
    assert ys["yearly_inflow_total_nis"] == pytest.approx(10000.0)
    # Top 12-month categories are spending-only.
    yr_slugs = [c["slug"] for c in ys["top_categories_12m"]]
    assert "income.salary" not in yr_slugs


def test_dashboard_overview_month_param_rescopes_current(client_with_db):
    """?month=YYYY-MM rescopes current_month_* to the requested month even
    when newer months exist (or when it's not the latest)."""
    SF = client_with_db.app.state.session_factory
    user_id = "u_month"
    _seed_inflow_and_spend(
        client_with_db, user_id=user_id, month=(2026, 4),
    )
    # Add ALSO a May 2026 row so April is no longer the latest.
    with SF() as s:
        src = s.query(ExpenseSource).filter_by(user_id=user_id).one()
        stmt = s.query(ExpenseStatement).filter_by(user_id=user_id).one()
        cat = s.query(ExpenseCategory).filter_by(
            user_id=user_id, slug="dining_out.restaurants",
        ).one()
        s.add(ExpenseTransaction(
            user_id=user_id, source_id=src.id, statement_id=stmt.id,
            occurred_on=date(2026, 5, 10),
            merchant_raw="MAY RESTO", merchant_normalized="may resto",
            amount_nis=Decimal("999"),
            direction="debit", tx_type="regular",
            category_id=cat.id, category_source="rule",
            category_confidence=Decimal("1.0"), raw_row_json="{}",
        ))
        s.commit()
    # Default (no month=): focal=2026-05, spending=999.
    r = client_with_db.get(
        f"/api/expenses/dashboard-overview?user_id={user_id}&months=12"
    )
    assert r.status_code == 200
    assert r.json()["current_month"] == "2026-05"
    assert r.json()["current_month_spending_nis"] == pytest.approx(999.0)
    # With month=2026-04: focal=2026-04, spending=200 + inflow=10000.
    r = client_with_db.get(
        f"/api/expenses/dashboard-overview?user_id={user_id}&months=12&month=2026-04"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["current_month"] == "2026-04"
    assert body["current_month_spending_nis"] == pytest.approx(200.0)
    assert body["current_month_inflow_nis"] == pytest.approx(10000.0)


def test_dashboard_overview_yearly_split_inflow_and_spending(client_with_db):
    """yearly_summary exposes both numbers non-zero with the right split."""
    _seed_inflow_and_spend(client_with_db, user_id="u_yr_split")
    r = client_with_db.get(
        "/api/expenses/dashboard-overview?user_id=u_yr_split&months=12"
    )
    body = r.json()
    ys = body["yearly_summary"]
    assert ys["yearly_spending_total_nis"] > 0
    assert ys["yearly_inflow_total_nis"] > 0
    # They are NOT equal — strong separation:
    assert ys["yearly_spending_total_nis"] != ys["yearly_inflow_total_nis"]
    assert ys["yearly_spending_total_nis"] == pytest.approx(200.0)
    assert ys["yearly_inflow_total_nis"] == pytest.approx(10000.0)
