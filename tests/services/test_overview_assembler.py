"""Unit tests for the Overview plain-language plan-explainer assembler
(``argosy/services/overview_assembler.py``), per the design spec
``docs/superpowers/specs/2026-06-21-overview-plan-explainer-design.md`` §6.

These are pure UNIT tests: every chapter builder is exercised against a fake
``resolved`` object built from the real :class:`ResolvedValue` dataclass, so no
DB / FastAPI is required. The contract under test:

  * static templates carry no hand-typed magnitudes (leak gate);
  * the FI chapter branches on the SIGN of the margin and renders the absolute
    magnitude with no minus sign;
  * ``_build_fi_series`` uses the deterministic SCALAR FV path (B1 untouched);
  * the NVDA chapter caps the "your move" sell to eligible-now shares;
  * pending facts degrade a chapter instead of fabricating a number;
  * the RSU chapter falls back cleanly with no magnitudes when degraded.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from argosy.services.plan_numeric_resolver import ResolvedPlanNumbers, ResolvedValue
from argosy.quality.fact_registry import find_unauthorized_numbers, render_fact

import argosy.services.overview_assembler as oa


# ---------------------------------------------------------------------------
# Helpers — build a real ResolvedPlanNumbers from a {key: (value, unit)} map.
# A key mapped to None value (or simply absent) stays a pending sentinel, so the
# builders see exactly the "degrade, don't crash" surface they would in prod.
# ---------------------------------------------------------------------------
def _resolved(values: dict[str, tuple[float | None, str]]) -> ResolvedPlanNumbers:
    out: dict[str, ResolvedValue] = {}
    for key, (val, unit) in values.items():
        if val is None:
            out[key] = ResolvedValue.pending(key, unit, f"{key} (pending)")
        else:
            out[key] = ResolvedValue(
                key=key,
                value=float(val),
                unit=unit,
                status="resolved",
                source_locator=f"test.{key}",
                confidence="HIGH",
            )
    return ResolvedPlanNumbers(values=out)


# Common resolved fixtures reused across chapters. Liquid is SHORT of FI total
# (negative margin) — the live pv62 sign.
def _fi_resolved(*, margin: float | None = -168_000.0, crossing: float | None = 2027.0):
    return _resolved(
        {
            "retirement.fi_total_capital_nis": (11_836_000.0, "nis"),
            "portfolio.liquid_net_worth_nis": (11_668_000.0, "nis"),
            "retirement.fi_margin_signed_nis": (margin, "nis"),
            "retirement.fi_crossing_year": (crossing, "year"),
            "retirement.return_assumption_pct": (0.05, "pct"),
            "savings.annual_net_nis": (500_000.0, "nis"),
        }
    )


# ---------------------------------------------------------------------------
# Test 1 — every static template passes the leak gate.
# ---------------------------------------------------------------------------
def test_static_templates_have_no_unauthorized_numbers():
    # Plain no-marker templates: must be clean as written.
    for name in (
        "_T_FI_REACHED",
        "_T_LIQUIDITY",
        "_T_ALLOCATION",
        "_T_NVDA",
        "_T_RSU_DEGRADED",
        "_T_PHASES",
        "_T_DUAL_TRACK",
    ):
        tmpl = getattr(oa, name)
        assert find_unauthorized_numbers(tmpl) == [], f"{name} leaks a magnitude"


def test_marker_templates_clean_when_markers_blanked():
    # The runtime-marker templates carry {SHORT_AMOUNT}/{FIRST_YEAR_NET}/... that
    # get filled with fact_registry-rendered strings; blanked they must be clean.
    for name in ("_T_FI_SHORT", "_T_RSU"):
        tmpl = getattr(oa, name)
        bare = (
            tmpl.replace("{SHORT_AMOUNT}", "")
            .replace("{FIRST_YEAR_NET}", "")
            .replace("{LAST_YEAR_NET}", "")
            .replace("{LAST_YEAR}", "")
        )
        assert find_unauthorized_numbers(bare) == [], f"{name} leaks a magnitude"


def test_assert_clean_templates_does_not_raise():
    # The import-time guard, called explicitly.
    oa._assert_clean_templates()


# ---------------------------------------------------------------------------
# Test 2 — _chapter_fi: sign branching + absolute magnitude + degrade.
# ---------------------------------------------------------------------------
def test_chapter_fi_negative_margin_says_short_and_no_minus_sign():
    resolved = _fi_resolved(margin=-168_000.0)
    ch = oa._chapter_fi(resolved)

    assert ch.id == "fi"
    assert "short" in ch.headline.lower()
    # The rendered absolute magnitude must appear WITHOUT a leading minus sign.
    short_abs = oa.render_signed_abs("retirement.fi_margin_signed_nis", resolved)
    assert short_abs in ch.headline
    assert "-" not in short_abs and "−" not in short_abs
    assert "₪" in short_abs  # magnitude actually rendered, not the raw token
    assert "{{fact:" not in ch.headline
    assert ch.degraded is False
    # Viz wired with a non-empty series + progress.
    assert ch.viz.kind == "fi_crossing"
    assert ch.viz.data["series"], "fi series should be non-empty"
    assert ch.viz.data["progress_pct"] is not None


def test_chapter_fi_positive_margin_says_reached_past_the_line():
    resolved = _fi_resolved(margin=250_000.0, crossing=2026.0)
    # Push liquid above the target so progress_pct is sensible too.
    resolved.values["portfolio.liquid_net_worth_nis"] = ResolvedValue(
        key="portfolio.liquid_net_worth_nis", value=12_086_000.0, unit="nis",
        status="resolved", source_locator="t",
    )
    ch = oa._chapter_fi(resolved)
    low = ch.headline.lower()
    assert "reached" in low or "past the line" in low
    assert "{{fact:" not in ch.headline
    assert ch.degraded is False


def test_chapter_fi_pending_margin_degrades_without_fabricating():
    resolved = _fi_resolved(margin=None)
    ch = oa._chapter_fi(resolved)
    assert ch.degraded is True
    # No fabricated magnitude for the short amount: the unresolved token stays.
    assert "{{fact:retirement.fi_margin_signed_nis}}" in ch.headline


# ---------------------------------------------------------------------------
# Test 3 — _build_fi_series: scalar FV path, monotonic, >=6 points, current year.
# ---------------------------------------------------------------------------
def test_build_fi_series_scalar_path_matches_future_value():
    from argosy.services.fi_crossing import _future_value

    resolved = _fi_resolved(crossing=2027.0)
    data, ok = oa._build_fi_series(resolved)
    assert ok is True
    series = data["series"]
    assert len(series) >= oa._FI_SERIES_MIN_POINTS
    current_year = datetime.now().year
    assert series[0]["year"] == current_year

    liquid = 11_668_000.0
    ret = 0.05
    savings = 500_000.0
    # Must equal the SCALAR fi_crossing FV (no savings_by_year vector).
    for n, point in enumerate(series):
        expected = _future_value(liquid, ret, savings, n)
        assert point["projected_liquid_nis"] == pytest.approx(expected)
        assert point["year"] == current_year + n

    # Increasing (positive return + positive savings).
    proj = [p["projected_liquid_nis"] for p in series]
    assert all(b >= a for a, b in zip(proj, proj[1:]))
    assert data["progress_pct"] == pytest.approx(liquid / 11_836_000.0 * 100.0)
    assert data["target_nis"] == pytest.approx(11_836_000.0)


def test_build_fi_series_pending_input_is_degraded_and_empty():
    resolved = _fi_resolved()
    # Knock out one required input.
    resolved.values["savings.annual_net_nis"] = ResolvedValue.pending(
        "savings.annual_net_nis", "nis", "pending"
    )
    data, ok = oa._build_fi_series(resolved)
    assert ok is False
    assert data["series"] == []


# ---------------------------------------------------------------------------
# Test 4 — _chapter_nvda: your_move capped to eligible, share formatting, degrade.
# ---------------------------------------------------------------------------
def _nvda_resolved(*, sell=8_000.0, eligible=3_500.0, current=0.6708, target=0.12):
    return _resolved(
        {
            "concentration.nvda_current_pct": (current, "pct"),
            "concentration.nvda_target_pct": (target, "pct"),
            "concentration.nvda_cap_pct": (0.13, "pct"),
            "concentration.nvda_eligible_now_sh": (eligible, "sh"),
            "concentration.nvda_sell_sh": (sell, "sh"),
            "concentration.nvda_target_sh": (5_000.0, "sh"),
        }
    )


def test_chapter_nvda_your_move_capped_to_eligible():
    resolved = _nvda_resolved(sell=8_000.0, eligible=3_500.0)
    ch = oa._chapter_nvda(resolved)
    assert ch.degraded is False
    assert ch.your_move is not None
    # Capped: sell (8000) > eligible (3500) -> label uses the eligible 3,500.
    assert "3,500" in ch.your_move.label
    assert "8,000" not in ch.your_move.label
    assert ch.your_move.href == "/proposals"
    assert ch.viz.kind == "nvda_winddown"
    assert ch.viz.data["eligible_now_sh"] == pytest.approx(3_500.0)


def test_chapter_nvda_pending_facts_degrade():
    resolved = _nvda_resolved()
    resolved.values["concentration.nvda_current_pct"] = ResolvedValue.pending(
        "concentration.nvda_current_pct", "pct", "pending"
    )
    ch = oa._chapter_nvda(resolved)
    assert ch.degraded is True
    assert "{{fact:concentration.nvda_current_pct}}" in ch.headline


# ---------------------------------------------------------------------------
# Test 5 — _chapter_dual_track, _chapter_liquidity: headline + degrade + viz.
# ---------------------------------------------------------------------------
def test_chapter_dual_track_renders_ages():
    resolved = _resolved(
        {
            "retirement.earliest_safe_age": (47.0, "age"),
            "retirement.preservation_age": (54.0, "age"),
        }
    )
    ch = oa._chapter_dual_track(resolved)
    assert ch.degraded is False
    assert render_fact("retirement.earliest_safe_age", resolved) in ch.headline
    assert render_fact("retirement.preservation_age", resolved) in ch.headline
    assert ch.viz.kind == "dual_track_age"
    assert ch.viz.data["earliest_safe_age"] == pytest.approx(47.0)
    assert ch.viz.data["preservation_age"] == pytest.approx(54.0)


def test_chapter_dual_track_pending_degrades():
    resolved = _resolved(
        {
            "retirement.earliest_safe_age": (None, "age"),
            "retirement.preservation_age": (54.0, "age"),
        }
    )
    ch = oa._chapter_dual_track(resolved)
    assert ch.degraded is True
    assert "{{fact:retirement.earliest_safe_age}}" in ch.headline


def test_chapter_liquidity_renders_and_derives_illiquid():
    resolved = _resolved(
        {
            "portfolio.total_net_worth_incl_residence_nis": (14_050_000.0, "nis"),
            "portfolio.liquid_net_worth_nis": (11_668_000.0, "nis"),
        }
    )
    ch = oa._chapter_liquidity(resolved, illiquid_nis=None)
    assert ch.degraded is False
    assert ch.viz.kind == "liquid_split"
    assert ch.viz.data["liquid_nis"] == pytest.approx(11_668_000.0)
    assert ch.viz.data["total_nis"] == pytest.approx(14_050_000.0)
    # illiquid derived = total - liquid.
    assert ch.viz.data["illiquid_nis"] == pytest.approx(14_050_000.0 - 11_668_000.0)
    assert "{{fact:" not in ch.headline


def test_chapter_liquidity_pending_degrades():
    resolved = _resolved(
        {
            "portfolio.total_net_worth_incl_residence_nis": (None, "nis"),
            "portfolio.liquid_net_worth_nis": (11_668_000.0, "nis"),
        }
    )
    ch = oa._chapter_liquidity(resolved, illiquid_nis=None)
    assert ch.degraded is True


# ---------------------------------------------------------------------------
# Test 6 — _chapter_rsu: degraded fallback vs a years list.
# ---------------------------------------------------------------------------
def test_chapter_rsu_degraded_when_no_years():
    ch = oa._chapter_rsu([], degraded_reason=None)
    assert ch.degraded is True
    assert ch.facts == []
    # Fallback headline carries no magnitude.
    assert find_unauthorized_numbers(ch.headline) == []
    assert "{{fact:" not in ch.headline


def test_chapter_rsu_degraded_reason_uses_fallback():
    ch = oa._chapter_rsu(
        [{"year": 2026, "net_nis": 100_000.0}],
        degraded_reason="missing NVDA price",
    )
    assert ch.degraded is True
    assert ch.facts == []
    assert find_unauthorized_numbers(ch.headline) == []


def test_chapter_rsu_with_years_renders_first_and_last():
    from argosy.quality.fact_registry import format_fact

    years = [
        {"year": 2026, "net_nis": 420_000.0},
        {"year": 2027, "net_nis": 310_000.0},
        {"year": 2030, "net_nis": 90_000.0},
    ]
    ch = oa._chapter_rsu(years, degraded_reason=None)
    assert ch.degraded is False
    # First/last year amounts + last year present in headline.
    assert format_fact(420_000.0, "nis", display="nis") in ch.headline
    assert format_fact(90_000.0, "nis", display="nis") in ch.headline
    assert format_fact(2030, "year", display="year") in ch.headline
    assert "{{fact:" not in ch.headline
    # Per-year FactRefData marked display-only.
    assert len(ch.facts) == 3
    for f in ch.facts:
        assert "display only" in f.source_locator
        assert f.status == "resolved"
    assert ch.viz.kind == "rsu_forward"
    assert ch.viz.data["years"] == years
