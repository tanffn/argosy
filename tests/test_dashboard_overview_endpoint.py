"""Dashboard-overview endpoint — yearly tab payload.

After EX6 the endpoint scopes strictly to "how is the year going?" — no
``current_month_*`` headlines, no top merchants, no anomalies (those moved
to ``GET /api/expenses/dashboard-monthly``). The asserted shape is:

    months[]            yearly_summary
    savings_rate_trend  top_movers       currency_mix
    dividends|null      taxes|null       sources_health
    fx_mode
"""

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


def test_dashboard_overview_returns_yearly_payload(client_with_db):
    """The new shape has yearly aggregates only — no current_month_* fields."""
    _seed_minimal(client_with_db)
    r = client_with_db.get("/api/expenses/dashboard-overview?user_id=u1&months=6")
    assert r.status_code == 200
    body = r.json()
    assert "months" in body
    assert "yearly_summary" in body
    assert "savings_rate_trend" in body
    assert "top_movers" in body
    assert "currency_mix" in body
    assert "dividends" in body
    assert "taxes" in body
    assert "sources_health" in body
    assert "fx_mode" in body
    # Removed fields must NOT be present.
    for removed in (
        "current_month",
        "current_month_spending_nis",
        "current_month_income_nis",
        "current_month_refunds_nis",
        "current_month_inflow_nis",
        "current_month_top_categories",
        "current_month_income",
        "current_month_inflow",
        "top_merchants_current_month",
        "anomalies",
    ):
        assert removed not in body, f"{removed!r} should be gone after EX6"


def test_dashboard_overview_empty_corpus_returns_empty_lists(client_with_db):
    r = client_with_db.get("/api/expenses/dashboard-overview?user_id=u_empty&months=6")
    assert r.status_code == 200
    body = r.json()
    assert body["months"] == []
    assert body["sources_health"] == []
    assert body["savings_rate_trend"] == []
    assert body["currency_mix"] == []
    # top_movers always renders, just with empty grew/shrank when no data.
    assert body["top_movers"]["grew"] == []
    assert body["top_movers"]["shrank"] == []
    # No dividend/tax data → those fields are nullable.
    assert body["dividends"] is None
    assert body["taxes"] is None


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
    """yearly_summary is the 12-month rollup that powers the 'Bottom line' card."""
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
    # current_vs_avg_pct is None now that focal-month is gone.
    assert ys["current_vs_avg_pct"] is None
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


# ---------------------------------------------------------------------------
# Inflow vs spending separation (yearly only — current_month_* moved out)
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


def test_dashboard_overview_yearly_separates_inflow_from_spending(client_with_db):
    """SALARY (inflow) belongs in yearly_inflow_total_nis, NOT in
    yearly_spending_total_nis or top_categories_12m. Dining (debit) is
    spending."""
    _seed_inflow_and_spend(client_with_db, user_id="u_sep")
    r = client_with_db.get(
        "/api/expenses/dashboard-overview?user_id=u_sep&months=6"
    )
    assert r.status_code == 200
    body = r.json()
    ys = body["yearly_summary"]
    assert ys["yearly_spending_total_nis"] == pytest.approx(200.0)
    assert ys["yearly_inflow_total_nis"] == pytest.approx(10000.0)
    yr_slugs = [c["slug"] for c in ys["top_categories_12m"]]
    assert "dining_out.restaurants" in yr_slugs
    assert "income.salary" not in yr_slugs


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
    """top_categories_12m returns ALL categories with non-zero spend, sorted
    desc — not just top 5."""
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
# Refund vs income split (yearly only)
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


def test_dashboard_overview_yearly_splits_income_from_refunds(client_with_db):
    _seed_income_and_refund(client_with_db, user_id="u_inc_ref")
    r = client_with_db.get(
        "/api/expenses/dashboard-overview?user_id=u_inc_ref&months=6"
    )
    assert r.status_code == 200
    body = r.json()
    ys = body["yearly_summary"]
    assert ys["yearly_income_total_nis"] == pytest.approx(10000.0)
    assert ys["yearly_refunds_total_nis"] == pytest.approx(50.0)
    assert ys["yearly_inflow_total_nis"] == pytest.approx(10050.0)


def test_dashboard_overview_months_chart_excludes_inflows_and_investments(
    client_with_db,
):
    """The "Monthly spend" chart series (`body["months"]`) is SPENDING-ONLY.

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


# ---------------------------------------------------------------------------
# New EX6 fields: savings_rate_trend, top_movers, currency_mix
# ---------------------------------------------------------------------------


def test_dashboard_overview_savings_rate_trend_shape(client_with_db):
    """savings_rate_trend has 12 oldest-first points when there is data."""
    _seed_inflow_and_spend(client_with_db, user_id="u_savings")
    r = client_with_db.get(
        "/api/expenses/dashboard-overview?user_id=u_savings&months=12"
    )
    body = r.json()
    pts = body["savings_rate_trend"]
    assert len(pts) == 12
    # oldest-first
    assert pts[0]["month"] < pts[-1]["month"]
    # Each point has the schema fields.
    p0 = pts[0]
    assert {"month", "income_nis", "spending_nis", "savings_rate"} <= set(p0.keys())
    # Data month (May 2026) should land at the right end of the trend.
    last = pts[-1]
    assert last["month"] == "2026-05"
    assert last["income_nis"] == pytest.approx(10000.0)
    assert last["spending_nis"] == pytest.approx(200.0)
    # rate = (income - spending)/income = (10000-200)/10000 = 0.98
    assert last["savings_rate"] == pytest.approx(0.98)


def test_dashboard_overview_top_movers_shape(client_with_db):
    """top_movers always renders even with empty/short history."""
    r = client_with_db.get(
        "/api/expenses/dashboard-overview?user_id=u_void3&months=12"
    )
    body = r.json()
    tm = body["top_movers"]
    assert tm["grew"] == []
    assert tm["shrank"] == []
    # No data → reason='insufficient_history'.
    assert tm["reason"] == "insufficient_history"


def test_dashboard_overview_currency_mix_shape(client_with_db):
    """currency_mix returns 12 points when there is data."""
    _seed_inflow_and_spend(client_with_db, user_id="u_ccy")
    r = client_with_db.get(
        "/api/expenses/dashboard-overview?user_id=u_ccy&months=12"
    )
    body = r.json()
    pts = body["currency_mix"]
    assert len(pts) == 12
    # Each entry has month/nis/usd
    p0 = pts[0]
    assert {"month", "nis", "usd"} <= set(p0.keys())
    # The data month bucket should hold 200 NIS (dining); USD = 0.
    last = pts[-1]
    assert last["month"] == "2026-05"
    assert last["nis"] == pytest.approx(200.0)
    assert last["usd"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Dividends + Taxes (yearly only — focal-month detail moved to dashboard-monthly)
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


def test_dashboard_dividends_summary_yearly(client_with_db):
    _seed_dividends_and_taxes(client_with_db, user_id="u_div")
    r = client_with_db.get(
        "/api/expenses/dashboard-overview?user_id=u_div&months=12"
    )
    assert r.status_code == 200
    body = r.json()
    div = body["dividends"]
    assert div is not None
    # 125.50 + 75 = 200.50; non-dividend wire NOT counted.
    assert div["yearly_total_usd"] == pytest.approx(200.50)
    # monthly_series: one entry for 2026-05 only (the only dividend month).
    assert len(div["monthly_series"]) == 1
    assert div["monthly_series"][0]["month"] == "2026-05"
    assert div["monthly_series"][0]["total_usd"] == pytest.approx(200.50)
    # trend_12mo populated, 12 oldest-first entries.
    assert len(div["trend_12mo"]) == 12
    assert div["trend_12mo"][0]["month"] < div["trend_12mo"][-1]["month"]
    # The 2026-05 entry in the trend has total_usd = 200.50.
    by_month = {pt["month"]: pt for pt in div["trend_12mo"]}
    assert by_month["2026-05"]["total_usd"] == pytest.approx(200.50)
    # Months without dividends report 0.0
    earlier = div["trend_12mo"][0]
    assert earlier["total_usd"] == 0.0


def test_dashboard_taxes_summary_yearly(client_with_db):
    _seed_dividends_and_taxes(client_with_db, user_id="u_tax")
    r = client_with_db.get(
        "/api/expenses/dashboard-overview?user_id=u_tax&months=12"
    )
    body = r.json()
    tax = body["taxes"]
    assert tax is not None
    # 1200 (property) + 1000 (income tax) = 2200.
    assert tax["yearly_total_nis"] == pytest.approx(2200.0)
    # No Schwab CSV path env var set, so USD = 0.
    assert tax["yearly_total_usd"] == 0.0
    assert tax["by_kind"]["property_tax"] == pytest.approx(1200.0)
    assert tax["by_kind"]["income_tax_paid"] == pytest.approx(1000.0)
    # rsu_withholding_usd should NOT be present since CSV is absent.
    assert "rsu_withholding_usd" not in tax["by_kind"]
    # trend_12mo populated, 12 oldest-first entries.
    assert len(tax["trend_12mo"]) == 12
    assert tax["trend_12mo"][0]["month"] < tax["trend_12mo"][-1]["month"]
    # The 2026-05 entry in the trend has total_nis = 2200.
    by_month = {pt["month"]: pt for pt in tax["trend_12mo"]}
    assert by_month["2026-05"]["total_nis"] == pytest.approx(2200.0)


def test_dashboard_dividends_taxes_null_when_no_data(client_with_db):
    """No dividend / tax data → both fields are None (UI hides those cards)."""
    r = client_with_db.get(
        "/api/expenses/dashboard-overview?user_id=u_void2&months=12"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["dividends"] is None
    assert body["taxes"] is None


# ---------------------------------------------------------------------------
# Income-breakdown endpoint (sibling, unchanged)
# ---------------------------------------------------------------------------


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
