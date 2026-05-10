"""Tests for argosy.services.expense_dashboard aggregation helpers."""
from argosy.services import expense_dashboard


def test_module_importable():
    assert hasattr(expense_dashboard, "__name__")


def test_new_pydantic_types_exist():
    from argosy.api.routes.expenses import (
        SavingsRatePoint,
        CategoryDelta,
        TopMovers,
        CurrencyMixPoint,
        ChartWindowBar,
        HeroMetric,
        HeroStatsMonthly,
        CategoryDeviation,
        DashboardMonthly,
    )
    sr = SavingsRatePoint(month="2026-04", income_nis=100.0, spending_nis=80.0, savings_rate=0.2)
    assert sr.savings_rate == 0.2
    cd = CategoryDelta(slug="food", label="Food", current_nis=100.0, prior_nis=80.0, delta_nis=20.0, delta_pct=0.25)
    assert cd.delta_pct == 0.25
    tm = TopMovers(grew=[cd], shrank=[], reason=None)
    assert tm.reason is None
    cmp_ = CurrencyMixPoint(month="2026-04", nis=1000.0, usd=200.0)
    assert cmp_.usd == 200.0
    cwb = ChartWindowBar(month="2026-04", total_nis=1000.0, total_usd=0.0, is_padding=False, is_selected=True)
    assert cwb.is_selected is True
    hm = HeroMetric(value_nis=1000.0, mom_delta_pct=0.1, vs_trailing12_pct=0.05)
    assert hm.value_nis == 1000.0
    hsm = HeroStatsMonthly(
        spent=hm, income=hm, refunds=hm,
        statements_reconciled=3, anomalies_count=1,
    )
    assert hsm.statements_reconciled == 3
    cdv = CategoryDeviation(
        slug="food", label="Food", this_month_nis=3800.0,
        typical_mean_nis=2800.0, typical_std_nis=400.0, z_score=2.5, delta_pct=0.357,
    )
    assert cdv.z_score == 2.5
    dm = DashboardMonthly(
        month="2026-04", available_months=["2026-04"],
        chart_window=[cwb] * 12,
        hero_stats=hsm,
        top_categories=[], categories_vs_typical=[],
        top_merchants=[], largest_transactions=[],
        anomalies=[],
    )
    assert dm.month == "2026-04"


def test_compute_savings_rate_trend_basic(db_session_with_seeded_user):
    from argosy.services.expense_dashboard import compute_savings_rate_trend
    points = compute_savings_rate_trend(db_session_with_seeded_user, "test", months=12)
    assert len(points) == 12
    # oldest-first
    assert points[0].month < points[-1].month
    # rate is bounded
    for p in points:
        assert -10.0 <= p.savings_rate <= 1.0
    # Months without income should have rate == 0.0
    for p in points:
        if p.income_nis == 0:
            assert p.savings_rate == 0.0
    # Sanity: at least one income month and one zero-income month exist in
    # the 12-point window — fixture seeds alternating months across 14 total.
    assert any(p.income_nis > 0 for p in points)
    assert any(p.income_nis == 0 for p in points)


def test_compute_top_movers_trailing(db_session_with_seeded_user):
    from argosy.services.expense_dashboard import compute_top_movers
    movers = compute_top_movers(db_session_with_seeded_user, "test", window="trailing_12")
    assert movers.reason in (None, "insufficient_history")
    if movers.reason is None:
        assert len(movers.grew) <= 5
        assert len(movers.shrank) <= 5
        # grew is sorted desc by delta_nis
        for a, b in zip(movers.grew, movers.grew[1:]):
            assert a.delta_nis >= b.delta_nis
        # shrank is sorted asc by delta_nis (most-negative first)
        for a, b in zip(movers.shrank, movers.shrank[1:]):
            assert a.delta_nis <= b.delta_nis


def test_compute_top_movers_insufficient_history(db_session_short_history):
    """User with only 4 months of data → reason='insufficient_history'."""
    from argosy.services.expense_dashboard import compute_top_movers
    movers = compute_top_movers(db_session_short_history, "test", window="trailing_12")
    assert movers.reason == "insufficient_history"
    assert movers.grew == []
    assert movers.shrank == []
