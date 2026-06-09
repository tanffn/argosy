"""Unit tests for the canonical NVDA projection (chart-consistency fix).

The projection is the SINGLE source the /plan NVDA-trajectory chart and the
allocation glidepath both bind to, so the two surfaces can never again drift
apart. The math layer is pure (no DB, no clock): given today's tradeable
weight, the concentration cap, today's share count, the full-book weight, and
the planned annual sale flow, it emits one normalised selldown path
``norm(t) = shares(t) / today_shares`` plus the three denominators that the
surfaces render (tradeable weight, full-book weight, share count).

Canonical dev-DB inputs (plan v30 / drun 82) anchor the expectations:
current tradeable 64.86%, cap 13.0%, 11,471 shares, full-book 18.21%,
3,000 shares/yr planned flow.
"""
from __future__ import annotations

from datetime import date

from argosy.services.nvda_projection import build_nvda_projection


def _proj():
    return build_nvda_projection(
        today=date(2026, 6, 9),
        current_tradeable_pct=64.86,
        cap_pct=13.0,
        today_shares=11471,
        fullbook_current_pct=18.21,
        annual_reduction=3000,
    )


class TestTargetSharesMoneyMath:
    """E1=in-book, E2=flat-price → price cancels and the share target is just
    the weight ratio applied to today's count: floor(cap/current * today)."""

    def test_target_shares_is_floor_of_cap_over_current_times_today(self) -> None:
        # 13.0 / 64.86 * 11471 = 2299.15 -> floor -> 2299
        assert _proj().target_shares == 2299

    def test_target_norm_is_cap_over_current(self) -> None:
        p = _proj()
        assert abs(p.target_norm - (13.0 / 64.86)) < 1e-9


class TestAnchorAndEndpoints:
    def test_anchor_point_is_today_at_full_count(self) -> None:
        first = _proj().points[0]
        assert first.point_date == date(2026, 6, 9)
        assert first.shares == 11471
        assert abs(first.norm - 1.0) < 1e-9
        assert abs(first.tradeable_weight_pct - 64.86) < 1e-6
        assert abs(first.fullbook_weight_pct - 18.21) < 1e-6

    def test_endpoint_reaches_the_cap_exactly(self) -> None:
        p = _proj()
        last = p.points[-1]
        assert last.shares == 2299
        assert abs(last.tradeable_weight_pct - 13.0) < 1e-6

    def test_fullbook_target_is_the_basis_ratio_of_today(self) -> None:
        # 18.21 * (13.0/64.86) = 3.650
        p = _proj()
        assert abs(p.fullbook_target_pct - 3.650) < 0.005


class TestNormalisedPathInvariants:
    """Every surface is just `base * norm(t)` — this is what makes the
    cross-surface guardrail mechanically true."""

    def test_shares_equal_norm_times_today(self) -> None:
        for pt in _proj().points:
            assert pt.shares == round(pt.norm * 11471)

    def test_tradeable_weight_is_current_times_norm(self) -> None:
        for pt in _proj().points:
            assert abs(pt.tradeable_weight_pct - 64.86 * pt.norm) < 1e-6

    def test_fullbook_weight_is_fullbook_current_times_norm(self) -> None:
        for pt in _proj().points:
            assert abs(pt.fullbook_weight_pct - 18.21 * pt.norm) < 1e-6

    def test_path_is_monotone_non_increasing(self) -> None:
        pts = _proj().points
        for a, b in zip(pts, pts[1:]):
            assert b.norm <= a.norm + 1e-12
            assert b.shares <= a.shares


class TestFlowTimedDuration:
    """The plan's 3,000 sh/yr flow sets how long the glide takes; the target
    date is Argosy-derived, never a magic horizon."""

    def test_target_date_is_flow_timed(self) -> None:
        # (11471 - 2299) / 3000 = 3.057 yr -> ~1116 days after today
        p = _proj()
        years = (11471 - 2299) / 3000.0
        expected_days = round(years * 365.0)
        assert abs((p.target_date - date(2026, 6, 9)).days - expected_days) <= 1

    def test_points_flatten_at_cap_after_target_date(self) -> None:
        # If a horizon beyond the target is requested, NVDA holds at the cap.
        p = build_nvda_projection(
            today=date(2026, 6, 9),
            current_tradeable_pct=64.86,
            cap_pct=13.0,
            today_shares=11471,
            fullbook_current_pct=18.21,
            annual_reduction=3000,
            horizon_end=date(2032, 6, 9),
        )
        assert p.points[-1].point_date == date(2032, 6, 9)
        assert p.points[-1].shares == 2299
        assert abs(p.points[-1].tradeable_weight_pct - 13.0) < 1e-6
