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


def test_compute_currency_mix_basic(db_session_with_seeded_user):
    from argosy.services.expense_dashboard import compute_currency_mix
    points = compute_currency_mix(db_session_with_seeded_user, "test", months=12)
    assert len(points) == 12
    assert points[0].month < points[-1].month
    assert all(p.nis >= 0 for p in points)
    assert all(p.usd >= 0 for p in points)


def _shift_month(s: str, delta: int) -> str:
    y, m = int(s[:4]), int(s[5:7])
    m += delta
    while m > 12:
        m -= 12
        y += 1
    while m < 1:
        m += 12
        y -= 1
    return f"{y:04d}-{m:02d}"


def test_chart_window_centering_basic(db_session_long_history):
    """Selected month deep inside data range -> 6 before + 1 selected + 5 after."""
    from argosy.services.expense_dashboard import compute_chart_window
    bars = compute_chart_window(db_session_long_history, "test", focal_month="2025-06")
    assert len(bars) == 12
    months = [b.month for b in bars]
    assert months == [_shift_month("2025-06", -6 + i) for i in range(12)]
    assert sum(1 for b in bars if b.is_selected) == 1
    assert next(b for b in bars if b.is_selected).month == "2025-06"
    assert all(not b.is_padding for b in bars)


def test_chart_window_slide_at_future_edge(db_session_long_history):
    """Selected month is the latest month -> window slides left so newest is the rightmost bar."""
    from argosy.services.expense_dashboard import compute_chart_window
    # db_session_long_history's newest month is "2026-04"
    bars = compute_chart_window(db_session_long_history, "test", focal_month="2026-04")
    assert bars[-1].month == "2026-04"
    assert bars[-1].is_selected
    assert all(not b.is_padding for b in bars)


def test_chart_window_slide_at_past_edge(db_session_long_history):
    """Selected month is the earliest -> window slides right so oldest is leftmost bar."""
    from argosy.services.expense_dashboard import compute_chart_window
    # db_session_long_history's oldest month is "2024-12"
    bars = compute_chart_window(db_session_long_history, "test", focal_month="2024-12")
    assert bars[0].month == "2024-12"
    assert bars[0].is_selected
    assert all(not b.is_padding for b in bars)


def test_chart_window_short_history_pads_to_12(db_session_short_history):
    """User with 4 months of data -> 12 bars, 8 with is_padding=True."""
    from argosy.services.expense_dashboard import compute_chart_window
    bars = compute_chart_window(db_session_short_history, "test", focal_month="2026-03")
    assert len(bars) == 12
    pad_count = sum(1 for b in bars if b.is_padding)
    assert pad_count == 8


def test_compute_hero_stats_monthly_basic(db_session_with_seeded_user):
    from argosy.services.expense_dashboard import compute_hero_stats_monthly
    h = compute_hero_stats_monthly(db_session_with_seeded_user, "test", month="2026-03")
    assert h.spent.value_nis >= 0
    # When no prior month, mom_delta is None
    h_first = compute_hero_stats_monthly(db_session_with_seeded_user, "test", month="2024-12")
    assert h_first.spent.mom_delta_pct is None
    assert h_first.spent.vs_trailing12_pct is None


def test_compute_hero_stats_monthly_zero_prior(db_session_with_zero_prior):
    """If prior month had zero spending → mom_delta_pct is None (not infinity)."""
    from argosy.services.expense_dashboard import compute_hero_stats_monthly
    h = compute_hero_stats_monthly(db_session_with_zero_prior, "test", month="2026-04")
    assert h.spent.mom_delta_pct is None


def test_compute_categories_vs_typical_z_sort(db_session_with_seeded_user):
    from argosy.services.expense_dashboard import compute_categories_vs_typical
    out = compute_categories_vs_typical(db_session_with_seeded_user, "test", month="2026-03")
    assert len(out) <= 3
    for a, b in zip(out, out[1:]):
        assert abs(a.z_score) >= abs(b.z_score)
    for r in out:
        assert r.typical_std_nis >= 50.0


def test_compute_largest_transactions_top5_sorted(db_session_with_seeded_user):
    from argosy.services.expense_dashboard import compute_largest_transactions
    txs = compute_largest_transactions(db_session_with_seeded_user, "test", month="2026-03", limit=5)
    assert len(txs) <= 5
    for a, b in zip(txs, txs[1:]):
        assert abs(a.amount_nis or 0) >= abs(b.amount_nis or 0)


# ---------------- 12-month trend helpers (dividends, taxes) ----------------

def test_compute_dividends_trend_12mo_empty():
    """No data → empty list (caller decides whether to render anything)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from argosy.services.expense_dashboard import compute_dividends_trend_12mo
    from argosy.state.models import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        out = compute_dividends_trend_12mo(s, "nobody")
        assert out == []


def test_compute_taxes_trend_12mo_empty():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from argosy.services.expense_dashboard import compute_taxes_trend_12mo
    from argosy.state.models import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        out = compute_taxes_trend_12mo(s, "nobody")
        assert out == []


def test_compute_dividends_trend_12mo_basic(db_session_with_seeded_user):
    """Helper returns 12 oldest-first entries with `total_usd` populated for
    months that have USD-credit dividend rows. The seeded fixture doesn't
    include dividends, so all values should be 0.0."""
    from argosy.services.expense_dashboard import compute_dividends_trend_12mo
    out = compute_dividends_trend_12mo(db_session_with_seeded_user, "test")
    assert len(out) == 12
    assert out[0].month < out[-1].month
    # Each entry has the new TrendPoint shape — total_usd, total_nis (default 0).
    for pt in out:
        assert pt.total_usd >= 0.0
        assert pt.total_nis == 0.0


def test_compute_taxes_trend_12mo_basic(db_session_with_seeded_user):
    """Helper returns 12 oldest-first entries with `total_nis` populated for
    months that have tax-category debit rows."""
    from argosy.services.expense_dashboard import compute_taxes_trend_12mo
    out = compute_taxes_trend_12mo(db_session_with_seeded_user, "test")
    assert len(out) == 12
    assert out[0].month < out[-1].month
    for pt in out:
        assert pt.total_nis >= 0.0
        assert pt.total_usd == 0.0
