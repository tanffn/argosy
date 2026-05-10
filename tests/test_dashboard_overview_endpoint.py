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
        "yearly_spending_total_nis",
        "yearly_income_total_nis", "yearly_refunds_total_nis",
        "yearly_inflow_total_nis",
        "avg_per_month_nis",
        "top_categories_12m", "current_vs_avg_pct",
        "window", "window_label", "window_start_month", "window_end_month",
    }
    assert ys["months_covered"] == 1
    # total_nis is the (deprecated) alias for yearly_spending_total_nis.
    assert ys["total_nis"] == pytest.approx(250.0)
    assert ys["yearly_spending_total_nis"] == pytest.approx(250.0)
    assert ys["yearly_inflow_total_nis"] == pytest.approx(0.0)
    assert ys["yearly_income_total_nis"] == pytest.approx(0.0)
    assert ys["yearly_refunds_total_nis"] == pytest.approx(0.0)
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
    assert ys["yearly_income_total_nis"] == 0.0
    assert ys["yearly_refunds_total_nis"] == 0.0
    assert ys["avg_per_month_nis"] == 0.0
    assert ys["top_categories_12m"] == []
    assert ys["current_vs_avg_pct"] is None
    # Hero scalars are zero too.
    assert body["current_month"] is None
    assert body["current_month_spending_nis"] == 0.0
    assert body["current_month_inflow_nis"] == 0.0
    assert body["current_month_income_nis"] == 0.0
    assert body["current_month_refunds_nis"] == 0.0
    assert body["current_month_inflow"] == []
    assert body["current_month_income"] == []


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


def test_dashboard_overview_yearly_summary_window_calendar_year(client_with_db):
    """?window=calendar_year scopes the rollup to Jan-Dec of the year of the
    latest month with data. Default (?window=trailing_12) still works."""
    SF = client_with_db.app.state.session_factory
    user_id = "u_calyr"
    with SF() as s:
        s.add(User(id=user_id, plan="free")); s.flush()
        from argosy.services.expense_ingest.taxonomy_seed import (
            seed_system_defaults, seed_user_categories,
        )
        seed_system_defaults(s); s.flush()
        seed_user_categories(s, user_id); s.flush()
        f = UserFile(
            user_id=user_id, sha256="y"*64, original_name="x", sanitized_name="x",
            mime_type="x", kind="other", size_bytes=1, storage_path="/tmp/x",
            source="chat_attachment",
        )
        s.add(f); s.flush()
        src = ExpenseSource(
            user_id=user_id, kind="bank_account", issuer="leumi",
            external_id="0099", display_name="x",
        )
        s.add(src); s.flush()
        stmt = ExpenseStatement(
            user_id=user_id, source_id=src.id, file_id=f.id,
            period_start=date(2025, 1, 1), period_end=date(2026, 5, 31),
            parsed_total_nis=Decimal("0"),
            parser_name="x", parser_version="0.1.0", status="parsed",
        )
        s.add(stmt); s.flush()
        cat = s.query(ExpenseCategory).filter_by(
            user_id=user_id, slug="dining_out.restaurants",
        ).one()
        # 2025-12 = 100 NIS spending (will be EXCLUDED from calendar_year 2026
        # but included in trailing_12).
        s.add(ExpenseTransaction(
            user_id=user_id, source_id=src.id, statement_id=stmt.id,
            occurred_on=date(2025, 12, 5),
            merchant_raw="DEC TX", merchant_normalized="dec tx",
            amount_nis=Decimal("100"),
            direction="debit", tx_type="regular",
            category_id=cat.id, raw_row_json="{}",
        ))
        # 2026-03 = 50 NIS spending (in BOTH windows).
        s.add(ExpenseTransaction(
            user_id=user_id, source_id=src.id, statement_id=stmt.id,
            occurred_on=date(2026, 3, 5),
            merchant_raw="MAR TX", merchant_normalized="mar tx",
            amount_nis=Decimal("50"),
            direction="debit", tx_type="regular",
            category_id=cat.id, raw_row_json="{}",
        ))
        # 2026-05 = 30 NIS spending (in BOTH windows; this is "latest month").
        s.add(ExpenseTransaction(
            user_id=user_id, source_id=src.id, statement_id=stmt.id,
            occurred_on=date(2026, 5, 10),
            merchant_raw="MAY TX", merchant_normalized="may tx",
            amount_nis=Decimal("30"),
            direction="debit", tx_type="regular",
            category_id=cat.id, raw_row_json="{}",
        ))
        s.commit()
    # trailing_12 (default) — all three months in window.
    r = client_with_db.get(
        f"/api/expenses/dashboard-overview?user_id={user_id}&months=24"
    )
    assert r.status_code == 200
    ys = r.json()["yearly_summary"]
    assert ys["window"] == "trailing_12"
    assert ys["window_label"] == "Trailing 12 months"
    assert ys["window_start_month"] == "2025-12"
    assert ys["window_end_month"] == "2026-05"
    assert ys["yearly_spending_total_nis"] == pytest.approx(180.0)  # 100+50+30
    assert ys["months_covered"] == 3
    # calendar_year — only 2026 months in window.
    r = client_with_db.get(
        f"/api/expenses/dashboard-overview?user_id={user_id}&months=24"
        "&window=calendar_year"
    )
    ys = r.json()["yearly_summary"]
    assert ys["window"] == "calendar_year"
    assert ys["window_label"] == "2026 (calendar year)"
    assert ys["window_start_month"] == "2026-01"
    assert ys["window_end_month"] == "2026-05"
    assert ys["yearly_spending_total_nis"] == pytest.approx(80.0)  # 50+30
    assert ys["months_covered"] == 2  # mar + may


def test_dashboard_overview_yearly_summary_top_cats_returns_all(client_with_db):
    """top_categories_12m now returns ALL categories with non-zero spend
    sorted desc, not just top 5."""
    SF = client_with_db.app.state.session_factory
    user_id = "u_allcats"
    with SF() as s:
        s.add(User(id=user_id, plan="free")); s.flush()
        from argosy.services.expense_ingest.taxonomy_seed import (
            seed_system_defaults, seed_user_categories,
        )
        seed_system_defaults(s); s.flush()
        seed_user_categories(s, user_id); s.flush()
        f = UserFile(
            user_id=user_id, sha256="z"*64, original_name="x", sanitized_name="x",
            mime_type="x", kind="other", size_bytes=1, storage_path="/tmp/x",
            source="chat_attachment",
        )
        s.add(f); s.flush()
        src = ExpenseSource(
            user_id=user_id, kind="card", issuer="isracard",
            external_id="0099", display_name="x",
        )
        s.add(src); s.flush()
        stmt = ExpenseStatement(
            user_id=user_id, source_id=src.id, file_id=f.id,
            period_start=date(2026, 5, 1), period_end=date(2026, 5, 31),
            parsed_total_nis=Decimal("0"),
            parser_name="x", parser_version="0.1.0", status="parsed",
        )
        s.add(stmt); s.flush()
        # Seed 6 distinct categories with descending amounts so we can
        # assert >5 returned and ordering.
        slugs = [
            ("dining_out.restaurants", 600),
            ("food.groceries", 500),
            ("transportation.fuel", 400),
            ("transportation.parking", 300),
            ("subscriptions.streaming", 200),
            ("discretionary.shopping_clothing", 100),
        ]
        for slug, amt in slugs:
            cat = s.query(ExpenseCategory).filter_by(
                user_id=user_id, slug=slug,
            ).one_or_none()
            if cat is None:
                continue
            s.add(ExpenseTransaction(
                user_id=user_id, source_id=src.id, statement_id=stmt.id,
                occurred_on=date(2026, 5, 10),
                merchant_raw=f"M-{slug}", merchant_normalized=slug,
                amount_nis=Decimal(str(amt)),
                direction="debit", tx_type="regular",
                category_id=cat.id, raw_row_json="{}",
            ))
        s.commit()
    r = client_with_db.get(
        f"/api/expenses/dashboard-overview?user_id={user_id}&months=12"
    )
    body = r.json()
    cats = body["yearly_summary"]["top_categories_12m"]
    # >5 categories returned (was capped at 5 before).
    assert len(cats) > 5
    # Sorted desc.
    totals = [c["total_nis"] for c in cats]
    assert totals == sorted(totals, reverse=True)


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


# ---------------------------------------------------------------------------
# Refund vs income split (Feature 1)
# ---------------------------------------------------------------------------


def _seed_income_and_refund(client_with_db, *, user_id: str = "u_refund",
                             month: tuple[int, int] = (2026, 5)):
    """Seed: salary credit (10k, regular) + refund credit (50, refund)
    + dining debit (200) in one month.
    """
    SF = client_with_db.app.state.session_factory
    with SF() as s:
        s.add(User(id=user_id, plan="free")); s.flush()
        from argosy.services.expense_ingest.taxonomy_seed import (
            seed_system_defaults, seed_user_categories,
        )
        seed_system_defaults(s); s.flush()
        seed_user_categories(s, user_id); s.flush()
        f = UserFile(
            user_id=user_id, sha256="r"*64, original_name="x", sanitized_name="x",
            mime_type="x", kind="other", size_bytes=1, storage_path="/tmp/x",
            source="chat_attachment",
        )
        s.add(f); s.flush()
        src = ExpenseSource(
            user_id=user_id, kind="bank_account", issuer="leumi",
            external_id="9999", display_name="Leumi checking",
        )
        s.add(src); s.flush()
        y, m = month
        from calendar import monthrange
        last_day = monthrange(y, m)[1]
        stmt = ExpenseStatement(
            user_id=user_id, source_id=src.id, file_id=f.id,
            period_start=date(y, m, 1), period_end=date(y, m, last_day),
            parsed_total_nis=Decimal("10250"),
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
            occurred_on=date(y, m, 8),
            merchant_raw="REFUND BIG MEAL", merchant_normalized="refund big meal",
            amount_nis=Decimal("50"),
            direction="credit", tx_type="refund",
            category_id=dining_cat.id, category_source="rule",
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


def test_dashboard_overview_splits_income_from_refunds(client_with_db):
    _seed_income_and_refund(client_with_db, user_id="u_inc_ref")
    r = client_with_db.get(
        "/api/expenses/dashboard-overview?user_id=u_inc_ref&months=6"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["current_month_income_nis"] == pytest.approx(10000.0)
    assert body["current_month_refunds_nis"] == pytest.approx(50.0)
    assert body["current_month_inflow_nis"] == pytest.approx(10050.0)
    income_slugs = [c["slug"] for c in body["current_month_income"]]
    assert "income.salary" in income_slugs
    ys = body["yearly_summary"]
    assert ys["yearly_income_total_nis"] == pytest.approx(10000.0)
    assert ys["yearly_refunds_total_nis"] == pytest.approx(50.0)
    assert ys["yearly_inflow_total_nis"] == pytest.approx(10050.0)


def test_dashboard_overview_months_chart_excludes_inflows_and_investments(
    client_with_db,
):
    """The "Monthly spend" chart series (`body["months"]`) is SPENDING-ONLY.

    Previously the series summed all non-card-payment activity, so a month
    with a $40K investment buy + $151K RSU disbursement showed a giant USD
    bar that meant "money MOVED" — not "money spent". Fix: filter to
    direction='debit' AND category.is_inflow=False AND
    category.is_excluded_from_spend=False (mirrors current_month_top_categories).

    Seed: SALARY credit (10000, inflow) + investment debit (5000, excluded)
    + dining debit (200) all in May 2026. Expected: chart NIS bucket = 200.
    """
    SF = client_with_db.app.state.session_factory
    user_id = "u_chart"
    with SF() as s:
        s.add(User(id=user_id, plan="free")); s.flush()
        from argosy.services.expense_ingest.taxonomy_seed import (
            seed_system_defaults, seed_user_categories,
        )
        seed_system_defaults(s); s.flush()
        seed_user_categories(s, user_id); s.flush()
        f = UserFile(
            user_id=user_id, sha256="c"*64, original_name="x", sanitized_name="x",
            mime_type="x", kind="other", size_bytes=1, storage_path="/tmp/x",
            source="chat_attachment",
        )
        s.add(f); s.flush()
        src = ExpenseSource(
            user_id=user_id, kind="bank_account", issuer="leumi",
            external_id="0042", display_name="Leumi checking",
        )
        s.add(src); s.flush()
        stmt = ExpenseStatement(
            user_id=user_id, source_id=src.id, file_id=f.id,
            period_start=date(2026, 5, 1), period_end=date(2026, 5, 31),
            parsed_total_nis=Decimal("15200"),
            parser_name="leumi", parser_version="0.1.0", status="parsed",
        )
        s.add(stmt); s.flush()
        salary_cat = s.query(ExpenseCategory).filter_by(
            user_id=user_id, slug="income.salary",
        ).one()
        inv_cat = s.query(ExpenseCategory).filter_by(
            user_id=user_id, slug="investments.broker_buy_us",
        ).one()
        dining_cat = s.query(ExpenseCategory).filter_by(
            user_id=user_id, slug="dining_out.restaurants",
        ).one()
        # Salary credit (inflow → must NOT appear in chart).
        s.add(ExpenseTransaction(
            user_id=user_id, source_id=src.id, statement_id=stmt.id,
            occurred_on=date(2026, 5, 1),
            merchant_raw="SALARY", merchant_normalized="salary",
            amount_nis=Decimal("10000"),
            direction="credit", tx_type="regular",
            category_id=salary_cat.id, category_source="rule",
            category_confidence=Decimal("1.0"), raw_row_json="{}",
        ))
        # Investment debit (is_excluded_from_spend → must NOT appear in chart).
        s.add(ExpenseTransaction(
            user_id=user_id, source_id=src.id, statement_id=stmt.id,
            occurred_on=date(2026, 5, 10),
            merchant_raw="BROKER BUY", merchant_normalized="broker buy",
            amount_nis=Decimal("5000"),
            direction="debit", tx_type="regular",
            category_id=inv_cat.id, category_source="rule",
            category_confidence=Decimal("1.0"), raw_row_json="{}",
        ))
        # Dining debit — real spending → DOES appear in chart.
        s.add(ExpenseTransaction(
            user_id=user_id, source_id=src.id, statement_id=stmt.id,
            occurred_on=date(2026, 5, 15),
            merchant_raw="RESTO", merchant_normalized="resto",
            amount_nis=Decimal("200"),
            direction="debit", tx_type="regular",
            category_id=dining_cat.id, category_source="rule",
            category_confidence=Decimal("1.0"), raw_row_json="{}",
        ))
        s.commit()
    r = client_with_db.get(
        f"/api/expenses/dashboard-overview?user_id={user_id}&months=12"
    )
    assert r.status_code == 200
    body = r.json()
    # Exactly one month in series; chart NIS bucket = 200 (dining only).
    assert len(body["months"]) == 1
    assert body["months"][-1]["month"] == "2026-05"
    assert body["months"][-1]["totals_by_currency"]["NIS"] == pytest.approx(200.0)
    # Salary (credit) and broker buy (excluded_from_spend) both excluded.
    # Just one tx in the chart:
    assert body["months"][-1]["transaction_count"] == 1


def test_income_breakdown_endpoint(client_with_db):
    _seed_income_and_refund(client_with_db, user_id="u_inc_drill")
    r = client_with_db.get(
        "/api/expenses/income-breakdown?user_id=u_inc_drill&month=2026-05"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["month"] == "2026-05"
    assert body["total_nis"] == pytest.approx(10000.0)
    assert len(body["by_category"]) == 1
    assert body["by_category"][0]["slug"] == "income.salary"
    assert len(body["transactions"]) == 1
    assert body["transactions"][0]["merchant_raw"] == "SALARY DEPOSIT"


def test_income_breakdown_empty_month(client_with_db):
    _seed_income_and_refund(client_with_db, user_id="u_empty_inc")
    r = client_with_db.get(
        "/api/expenses/income-breakdown?user_id=u_empty_inc&month=2024-01"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total_nis"] == 0.0
    assert body["by_category"] == []
    assert body["transactions"] == []


# ---------------------------------------------------------------------------
# Dividends + Taxes (Feature 4)
# ---------------------------------------------------------------------------


def _seed_dividends_and_taxes(client_with_db, *, user_id: str = "u_div_tax"):
    """Seed: USD dividend credit + USD non-dividend credit + property-tax debit
    + income-tax debit, all in May 2026.
    """
    SF = client_with_db.app.state.session_factory
    with SF() as s:
        s.add(User(id=user_id, plan="free")); s.flush()
        from argosy.services.expense_ingest.taxonomy_seed import (
            seed_system_defaults, seed_user_categories,
        )
        seed_system_defaults(s); s.flush()
        seed_user_categories(s, user_id); s.flush()
        f = UserFile(
            user_id=user_id, sha256="d"*64, original_name="x", sanitized_name="x",
            mime_type="x", kind="other", size_bytes=1, storage_path="/tmp/x",
            source="chat_attachment",
        )
        s.add(f); s.flush()
        src_usd = ExpenseSource(
            user_id=user_id, kind="bank_account", issuer="leumi",
            external_id="9991-USD", display_name="Leumi USD",
        )
        s.add(src_usd); s.flush()
        src_nis = ExpenseSource(
            user_id=user_id, kind="bank_account", issuer="leumi",
            external_id="9992", display_name="Leumi NIS",
        )
        s.add(src_nis); s.flush()
        stmt_usd = ExpenseStatement(
            user_id=user_id, source_id=src_usd.id, file_id=f.id,
            period_start=date(2026, 5, 1), period_end=date(2026, 5, 31),
            parsed_total_nis=Decimal("0"),
            parser_name="leumi_usd", parser_version="0.1.0", status="parsed",
        )
        s.add(stmt_usd); s.flush()
        stmt_nis = ExpenseStatement(
            user_id=user_id, source_id=src_nis.id, file_id=f.id,
            period_start=date(2026, 5, 1), period_end=date(2026, 5, 31),
            parsed_total_nis=Decimal("2200"),
            parser_name="leumi", parser_version="0.1.0", status="parsed",
        )
        s.add(stmt_nis); s.flush()
        # Dividend (USD credit, English wording).
        s.add(ExpenseTransaction(
            user_id=user_id, source_id=src_usd.id, statement_id=stmt_usd.id,
            occurred_on=date(2026, 5, 10),
            merchant_raw="DIVIDEND XYZ", merchant_normalized="dividend xyz",
            amount_nis=None, amount_orig=Decimal("125.50"), currency_orig="USD",
            direction="credit", tx_type="regular",
            raw_row_json="{}",
        ))
        # Dividend (Hebrew Leumi wording).
        s.add(ExpenseTransaction(
            user_id=user_id, source_id=src_usd.id, statement_id=stmt_usd.id,
            occurred_on=date(2026, 5, 20),
            merchant_raw='נ"ע רבית/דו', merchant_normalized="נ\"ע רבית/דו",
            amount_nis=None, amount_orig=Decimal("75.00"), currency_orig="USD",
            direction="credit", tx_type="regular",
            raw_row_json="{}",
        ))
        # NOT a dividend — USD credit but no dividend wording.
        s.add(ExpenseTransaction(
            user_id=user_id, source_id=src_usd.id, statement_id=stmt_usd.id,
            occurred_on=date(2026, 5, 25),
            merchant_raw="WIRE FROM SCHWAB", merchant_normalized="wire from schwab",
            amount_nis=None, amount_orig=Decimal("200000.00"), currency_orig="USD",
            direction="credit", tx_type="regular",
            raw_row_json="{}",
        ))
        # Property tax (housing.property_tax, debit, NIS).
        prop_cat = s.query(ExpenseCategory).filter_by(
            user_id=user_id, slug="housing.property_tax",
        ).one()
        s.add(ExpenseTransaction(
            user_id=user_id, source_id=src_nis.id, statement_id=stmt_nis.id,
            occurred_on=date(2026, 5, 3),
            merchant_raw="ARNONA", merchant_normalized="arnona",
            amount_nis=Decimal("1200"),
            direction="debit", tx_type="regular",
            category_id=prop_cat.id, category_source="rule",
            category_confidence=Decimal("1.0"), raw_row_json="{}",
        ))
        # Income tax (taxes.income_tax_paid, debit, NIS).
        itp_cat = s.query(ExpenseCategory).filter_by(
            user_id=user_id, slug="taxes.income_tax_paid",
        ).one()
        s.add(ExpenseTransaction(
            user_id=user_id, source_id=src_nis.id, statement_id=stmt_nis.id,
            occurred_on=date(2026, 5, 5),
            merchant_raw="MAS HACHNASA", merchant_normalized="mas hachnasa",
            amount_nis=Decimal("1000"),
            direction="debit", tx_type="regular",
            category_id=itp_cat.id, category_source="rule",
            category_confidence=Decimal("1.0"), raw_row_json="{}",
        ))
        s.commit()


def test_dashboard_dividends_summary(client_with_db):
    _seed_dividends_and_taxes(client_with_db, user_id="u_div")
    r = client_with_db.get(
        "/api/expenses/dashboard-overview?user_id=u_div&months=12"
    )
    assert r.status_code == 200
    body = r.json()
    assert "dividends" in body
    div = body["dividends"]
    assert div["month"] == "2026-05"
    # 125.50 + 75 = 200.50; non-dividend wire NOT counted.
    assert div["current_month_total_usd"] == pytest.approx(200.50)
    assert div["yearly_total_usd"] == pytest.approx(200.50)
    # monthly_series: one entry for 2026-05 only.
    assert len(div["monthly_series"]) == 1
    assert div["monthly_series"][0]["month"] == "2026-05"
    assert div["monthly_series"][0]["total_usd"] == pytest.approx(200.50)
    # Two transaction rows surfaced for the focal month.
    assert len(div["transactions"]) == 2


def test_dashboard_taxes_summary(client_with_db):
    _seed_dividends_and_taxes(client_with_db, user_id="u_tax")
    r = client_with_db.get(
        "/api/expenses/dashboard-overview?user_id=u_tax&months=12"
    )
    body = r.json()
    assert "taxes" in body
    tax = body["taxes"]
    # 1200 (property) + 1000 (income tax) = 2200.
    assert tax["yearly_total_nis"] == pytest.approx(2200.0)
    # No Schwab CSV path env var set, so USD = 0.
    assert tax["yearly_total_usd"] == 0.0
    assert tax["by_kind"]["property_tax"] == pytest.approx(1200.0)
    assert tax["by_kind"]["income_tax_paid"] == pytest.approx(1000.0)
    # rsu_withholding_usd should NOT be present since CSV is absent.
    assert "rsu_withholding_usd" not in tax["by_kind"]


def test_dashboard_dividends_empty_corpus(client_with_db):
    """No data at all — dividends/taxes still render zero/empty (no crash)."""
    r = client_with_db.get(
        "/api/expenses/dashboard-overview?user_id=u_void2&months=12"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["dividends"]["yearly_total_usd"] == 0.0
    assert body["dividends"]["transactions"] == []
    assert body["taxes"]["yearly_total_nis"] == 0.0
    assert body["taxes"]["yearly_total_usd"] == 0.0


# ---------------------------------------------------------------------------
# Anomaly oddities (Feature 5)
# ---------------------------------------------------------------------------


def _seed_oddities(client_with_db, *, user_id: str = "u_odd"):
    """Seed:
      * Merchant 'COFFEE': prior-12mo avg = 30; focal-month tx of 250 → spike.
      * Merchant 'NEW BIG': no prior activity; focal tx of 1000 → new high.
      * Merchant 'NEW SMALL': no prior activity, focal tx of 200 → NO flag.
    """
    SF = client_with_db.app.state.session_factory
    with SF() as s:
        s.add(User(id=user_id, plan="free")); s.flush()
        from argosy.services.expense_ingest.taxonomy_seed import (
            seed_system_defaults, seed_user_categories,
        )
        seed_system_defaults(s); s.flush()
        seed_user_categories(s, user_id); s.flush()
        f = UserFile(
            user_id=user_id, sha256="o"*64, original_name="x", sanitized_name="x",
            mime_type="x", kind="other", size_bytes=1, storage_path="/tmp/x",
            source="chat_attachment",
        )
        s.add(f); s.flush()
        src = ExpenseSource(
            user_id=user_id, kind="card", issuer="isracard",
            external_id="0007", display_name="Test",
        )
        s.add(src); s.flush()
        # 12 prior months of COFFEE @ 30 NIS — Jun 2025 → Apr 2026.
        prior_stmt = ExpenseStatement(
            user_id=user_id, source_id=src.id, file_id=f.id,
            period_start=date(2025, 6, 1), period_end=date(2026, 4, 30),
            parsed_total_nis=Decimal("330"),
            parser_name="isracard", parser_version="0.1.0", status="parsed",
        )
        s.add(prior_stmt); s.flush()
        cat = s.query(ExpenseCategory).filter_by(
            user_id=user_id, slug="dining_out.restaurants",
        ).one()
        for i, month in enumerate([
            (2025, 6), (2025, 7), (2025, 8), (2025, 9), (2025, 10), (2025, 11),
            (2025, 12), (2026, 1), (2026, 2), (2026, 3), (2026, 4),
        ]):
            yy, mm = month
            s.add(ExpenseTransaction(
                user_id=user_id, source_id=src.id, statement_id=prior_stmt.id,
                occurred_on=date(yy, mm, 10),
                merchant_raw=f"COFFEE-{i}", merchant_normalized="coffee",
                amount_nis=Decimal("30"), direction="debit", tx_type="regular",
                category_id=cat.id, raw_row_json="{}",
            ))
        # Focal month: May 2026.
        focal_stmt = ExpenseStatement(
            user_id=user_id, source_id=src.id, file_id=f.id,
            period_start=date(2026, 5, 1), period_end=date(2026, 5, 31),
            parsed_total_nis=Decimal("1450"),
            parser_name="isracard", parser_version="0.1.0", status="parsed",
        )
        s.add(focal_stmt); s.flush()
        # Coffee spike (250 vs avg 30 → ~8.3x).
        s.add(ExpenseTransaction(
            user_id=user_id, source_id=src.id, statement_id=focal_stmt.id,
            occurred_on=date(2026, 5, 15),
            merchant_raw="COFFEE BIG", merchant_normalized="coffee",
            amount_nis=Decimal("250"), direction="debit", tx_type="regular",
            category_id=cat.id, raw_row_json="{}",
        ))
        # New big merchant (>=500, never seen before).
        s.add(ExpenseTransaction(
            user_id=user_id, source_id=src.id, statement_id=focal_stmt.id,
            occurred_on=date(2026, 5, 20),
            merchant_raw="NEW BIG STORE",
            merchant_normalized="new big store",
            amount_nis=Decimal("1000"), direction="debit", tx_type="regular",
            category_id=cat.id, raw_row_json="{}",
        ))
        # New small merchant (<500, no flag).
        s.add(ExpenseTransaction(
            user_id=user_id, source_id=src.id, statement_id=focal_stmt.id,
            occurred_on=date(2026, 5, 22),
            merchant_raw="NEW SMALL", merchant_normalized="new small",
            amount_nis=Decimal("200"), direction="debit", tx_type="regular",
            category_id=cat.id, raw_row_json="{}",
        ))
        s.commit()


def test_anomalies_detect_merchant_spike(client_with_db):
    _seed_oddities(client_with_db, user_id="u_spike")
    r = client_with_db.get(
        "/api/expenses/dashboard-overview?user_id=u_spike&months=12"
    )
    body = r.json()
    kinds = [a["kind"] for a in body["anomalies"]]
    assert "merchant_spike" in kinds
    spike = next(a for a in body["anomalies"] if a["kind"] == "merchant_spike")
    assert "COFFEE" in spike["message"].upper() or "coffee" in spike["message"].lower()
    # Severity yellow per spec.
    assert spike["severity"] == "yellow"


def test_anomalies_detect_new_high_value_merchant(client_with_db):
    _seed_oddities(client_with_db, user_id="u_newhi")
    r = client_with_db.get(
        "/api/expenses/dashboard-overview?user_id=u_newhi&months=12"
    )
    body = r.json()
    kinds = [a["kind"] for a in body["anomalies"]]
    assert "new_high_value_merchant" in kinds
    new_hi = next(
        a for a in body["anomalies"] if a["kind"] == "new_high_value_merchant"
    )
    assert "BIG" in new_hi["message"]
    # Small new merchant should NOT show up.
    msgs = " ".join(a["message"] for a in body["anomalies"])
    assert "NEW SMALL" not in msgs
