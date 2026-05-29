"""Unit tests for ``apply_life_event_deltas`` — Spec D commit #2.

Pure-function tests; no DB.  LifeEvent fixtures use ``SimpleNamespace``
so we don't depend on the SQLAlchemy machinery (the function is
duck-typed against the documented attribute set).

Test surface:
  - Empty events list returns a copy of the input.
  - one_shot at exact projection_start, mid-horizon, before, at horizon edge.
  - recurring with various anchors (in-horizon, before-projection).
  - phase_change_start / phase_change_end boundary semantics.
  - delta_kind='none' is a no-op.
  - Sign convention: positive amount reduces expense series; negative
    increases.
  - Mixed events apply additively.
  - 7-case sign-flip helper matrix from spec §2.0.
  - 22-case scenario matrix from spec §7.1.
  - len(input) != horizon_months → ValueError.
  - Output is a NEW list (input not mutated).
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from argosy.services.cashflow_projection import (
    _apply_signed_delta_to_series,
    apply_life_event_deltas,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

PROJ_START = date(2026, 5, 29)
HORIZON = 360  # 30 years
FX = 3.7
FLAT_EXPENSE = 30_000.0  # NIS / month (matches spec worked examples)


def _flat_series(horizon: int = HORIZON, value: float = FLAT_EXPENSE) -> list[float]:
    return [value] * horizon


def _one_shot(target_date: date, amount_usd: float) -> SimpleNamespace:
    return SimpleNamespace(
        delta_kind="one_shot",
        target_date=target_date,
        one_shot_amount_usd=amount_usd,
        # Per-shape fields the writer leaves NULL — set to None so
        # accidental reads fail loudly.
        monthly_delta_usd=None,
        recurring_amount_usd=None,
        recurring_period_years=None,
        phase_start_date=None,
        phase_end_date=None,
    )


def _recurring(
    anchor_date: date,
    amount_usd: float,
    period_years: int,
) -> SimpleNamespace:
    return SimpleNamespace(
        delta_kind="recurring_every_n_years",
        target_date=anchor_date,
        recurring_amount_usd=amount_usd,
        recurring_period_years=period_years,
        one_shot_amount_usd=None,
        monthly_delta_usd=None,
        phase_start_date=None,
        phase_end_date=None,
    )


def _phase_start(start_date: date, monthly_delta_usd: float) -> SimpleNamespace:
    return SimpleNamespace(
        delta_kind="phase_change_start",
        phase_start_date=start_date,
        phase_end_date=None,
        monthly_delta_usd=monthly_delta_usd,
        target_date=None,
        one_shot_amount_usd=None,
        recurring_amount_usd=None,
        recurring_period_years=None,
    )


def _phase_end(
    start_date: date,
    end_date: date,
    monthly_delta_usd: float,
) -> SimpleNamespace:
    return SimpleNamespace(
        delta_kind="phase_change_end",
        phase_start_date=start_date,
        phase_end_date=end_date,
        monthly_delta_usd=monthly_delta_usd,
        target_date=None,
        one_shot_amount_usd=None,
        recurring_amount_usd=None,
        recurring_period_years=None,
    )


def _none_event() -> SimpleNamespace:
    return SimpleNamespace(
        delta_kind="none",
        target_date=None,
        phase_start_date=None,
        phase_end_date=None,
        monthly_delta_usd=None,
        one_shot_amount_usd=None,
        recurring_amount_usd=None,
        recurring_period_years=None,
    )


def _months_between_dates(start: date, target: date) -> int:
    return (target.year - start.year) * 12 + (target.month - start.month)


# ---------------------------------------------------------------------------
# Sign-flip helper — 7-case matrix from spec §2.0
# ---------------------------------------------------------------------------


class TestApplySignedDeltaToSeries:
    """The single sign-flip site.  Per spec §2.0 / codex BLOCKER #3."""

    def test_income_positive_fx_reduces_series(self):
        s = [0.0]
        _apply_signed_delta_to_series(s, 0, +200.0, 3.7)
        assert s[0] == pytest.approx(-740.0)

    def test_expense_positive_fx_increases_series(self):
        s = [0.0]
        _apply_signed_delta_to_series(s, 0, -200.0, 3.7)
        assert s[0] == pytest.approx(+740.0)

    def test_income_zero_fx_is_noop(self):
        s = [100.0]
        _apply_signed_delta_to_series(s, 0, +200.0, 0.0)
        assert s[0] == pytest.approx(100.0)

    def test_expense_zero_fx_is_noop(self):
        s = [100.0]
        _apply_signed_delta_to_series(s, 0, -200.0, 0.0)
        assert s[0] == pytest.approx(100.0)

    def test_zero_amount_is_noop(self):
        s = [100.0]
        _apply_signed_delta_to_series(s, 0, 0.0, 3.7)
        assert s[0] == pytest.approx(100.0)

    def test_income_on_negative_series(self):
        s = [-300.0]
        _apply_signed_delta_to_series(s, 0, +50.0, 3.7)
        # series[0] -= 50 * 3.7 = -185 from -300 → -485
        assert s[0] == pytest.approx(-485.0)

    def test_expense_on_positive_series(self):
        s = [300.0]
        _apply_signed_delta_to_series(s, 0, -50.0, 3.7)
        # series[0] += 50 * 3.7 = 185 from 300 → 485
        assert s[0] == pytest.approx(485.0)


# ---------------------------------------------------------------------------
# Empty / contract tests
# ---------------------------------------------------------------------------


class TestContract:
    def test_empty_events_returns_copy_of_input(self):
        base = _flat_series()
        out = apply_life_event_deltas(base, [], PROJ_START, HORIZON, FX)
        assert out == base

    def test_returns_new_list_not_input(self):
        base = _flat_series()
        out = apply_life_event_deltas(base, [], PROJ_START, HORIZON, FX)
        assert out is not base

    def test_input_not_mutated(self):
        base = _flat_series()
        snapshot = list(base)
        apply_life_event_deltas(
            base,
            [_one_shot(date(2030, 1, 1), -50_000.0)],
            PROJ_START,
            HORIZON,
            FX,
        )
        assert base == snapshot  # input untouched

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError, match="horizon_months"):
            apply_life_event_deltas(
                [1.0, 2.0, 3.0],
                [],
                PROJ_START,
                horizon_months=5,
                fx_usd_nis_for_event=FX,
            )

    def test_unknown_delta_kind_silently_skipped(self):
        weird = SimpleNamespace(delta_kind="future_unknown_shape")
        base = _flat_series()
        out = apply_life_event_deltas(base, [weird], PROJ_START, HORIZON, FX)
        assert out == base


# ---------------------------------------------------------------------------
# One-shot tests — spec §7.1 cases 2-7
# ---------------------------------------------------------------------------


class TestOneShot:
    def test_one_shot_at_offset_12(self):
        """§7.1 #2 — one_shot at +12 months modifies series[12]."""
        event = _one_shot(date(2027, 5, 15), -10_000.0)  # 12 months from PROJ_START
        out = apply_life_event_deltas(
            _flat_series(), [event], PROJ_START, HORIZON, FX
        )
        # -10000 USD signed (expense) → series[12] += 10000 * 3.7 = +37000
        assert out[12] == pytest.approx(FLAT_EXPENSE + 37_000.0)
        # Other months untouched.
        assert out[11] == pytest.approx(FLAT_EXPENSE)
        assert out[13] == pytest.approx(FLAT_EXPENSE)
        assert out[0] == pytest.approx(FLAT_EXPENSE)

    def test_one_shot_before_projection_start_skipped(self):
        """§7.1 #3 — past events have no effect."""
        event = _one_shot(date(2020, 1, 1), -50_000.0)
        out = apply_life_event_deltas(
            _flat_series(), [event], PROJ_START, HORIZON, FX
        )
        assert out == _flat_series()

    def test_one_shot_at_exact_projection_start(self):
        """§7.1 #4 — one_shot at projection_start lands in series[0]."""
        event = _one_shot(PROJ_START, -10_000.0)  # same month → offset 0
        out = apply_life_event_deltas(
            _flat_series(), [event], PROJ_START, HORIZON, FX
        )
        assert out[0] == pytest.approx(FLAT_EXPENSE + 37_000.0)
        assert out[1] == pytest.approx(FLAT_EXPENSE)

    def test_one_shot_at_horizon_edge_excluded(self):
        """§7.1 #5 — m_offset == horizon_months is OUT of range."""
        # horizon=360 from 2026-05; index 360 = 2056-05.
        event = _one_shot(date(2056, 5, 1), -100_000.0)
        out = apply_life_event_deltas(
            _flat_series(), [event], PROJ_START, HORIZON, FX
        )
        # Series should be unchanged.
        assert out == _flat_series()

    def test_one_shot_at_last_valid_horizon_index_included(self):
        """One past edge is excluded → the index BEFORE the edge is the
        last valid spot.  horizon=360, last index = 359."""
        # 2056-04 = 30y - 1 month from 2026-05 = offset 359
        event = _one_shot(date(2056, 4, 15), -1_000.0)
        out = apply_life_event_deltas(
            _flat_series(), [event], PROJ_START, HORIZON, FX
        )
        assert out[359] == pytest.approx(FLAT_EXPENSE + 3_700.0)
        assert out[358] == pytest.approx(FLAT_EXPENSE)

    def test_one_shot_at_month_boundary_first_day(self):
        """§7.1 #6 — date = first of month lands in that month."""
        event = _one_shot(date(2030, 1, 1), -1_000.0)
        out = apply_life_event_deltas(
            _flat_series(), [event], PROJ_START, HORIZON, FX
        )
        offset = _months_between_dates(PROJ_START, date(2030, 1, 1))
        assert out[offset] == pytest.approx(FLAT_EXPENSE + 3_700.0)

    def test_one_shot_at_month_boundary_last_day(self):
        """§7.1 #7 — date = last day of month lands in same month."""
        event = _one_shot(date(2030, 1, 31), -1_000.0)
        out = apply_life_event_deltas(
            _flat_series(), [event], PROJ_START, HORIZON, FX
        )
        offset = _months_between_dates(PROJ_START, date(2030, 1, 31))
        assert out[offset] == pytest.approx(FLAT_EXPENSE + 3_700.0)

    def test_one_shot_positive_amount_reduces_expense(self):
        """§7.1 #18 — positive amount = income → series[m] DECREASES."""
        event = _one_shot(date(2028, 11, 20), +200_000.0)  # inheritance
        out = apply_life_event_deltas(
            _flat_series(), [event], PROJ_START, HORIZON, FX
        )
        offset = _months_between_dates(PROJ_START, date(2028, 11, 20))
        # Spec Example C: series[30] should become 30000 - 740000 = -710000
        assert out[offset] == pytest.approx(FLAT_EXPENSE - 740_000.0)
        # Yes, negative — the projection engine treats that as a surplus
        # month per spec Example C commentary.

    def test_one_shot_spec_example_c_wedding(self):
        """Spec Appendix C: $50K wedding gift in 2031-06 → +185k NIS spike."""
        event = _one_shot(date(2031, 6, 10), -50_000.0)
        out = apply_life_event_deltas(
            _flat_series(), [event], PROJ_START, HORIZON, FX
        )
        # (2031-06) - (2026-05) = 61 months
        assert out[61] == pytest.approx(FLAT_EXPENSE + 185_000.0)


# ---------------------------------------------------------------------------
# Recurring tests — spec §7.1 cases 8-11
# ---------------------------------------------------------------------------


class TestRecurring:
    def test_recurring_period_1_year(self):
        """§7.1 #8 — period=1 fires every 12 months from anchor."""
        # Anchor 2030-09 (52 months out from 2026-05), period=1, amount -40k.
        event = _recurring(date(2030, 9, 1), -40_000.0, 1)
        out = apply_life_event_deltas(
            _flat_series(), [event], PROJ_START, HORIZON, FX
        )
        # Without end_date, fires every year until horizon.
        first_offset = _months_between_dates(PROJ_START, date(2030, 9, 1))
        spike = +148_000.0  # 40000 * 3.7
        for k in range(20):  # 20 occurrences fit in 30y horizon
            m = first_offset + k * 12
            if m >= HORIZON:
                break
            assert out[m] == pytest.approx(FLAT_EXPENSE + spike), f"k={k} m={m}"

    def test_recurring_spec_example_b_car(self):
        """Spec Appendix B: car every 5y, anchor 2027-Mar, -67k USD."""
        event = _recurring(date(2027, 3, 15), -67_000.0, 5)
        out = apply_life_event_deltas(
            _flat_series(), [event], PROJ_START, HORIZON, FX
        )
        spike = +247_900.0  # 67000 * 3.7
        # Expected offsets: 10, 70, 130, 190, 250, 310
        expected_offsets = [10, 70, 130, 190, 250, 310]
        for m in expected_offsets:
            assert out[m] == pytest.approx(
                FLAT_EXPENSE + spike
            ), f"car spike at offset {m}"
        # 370 is past horizon → should NOT fire.
        # All other months should be FLAT_EXPENSE.
        for m in range(HORIZON):
            if m in expected_offsets:
                continue
            assert out[m] == pytest.approx(FLAT_EXPENSE), f"unexpected delta at {m}"

    def test_recurring_anchor_before_projection_skips_to_first_in_horizon(self):
        """§7.1 #9 — anchor 3y BEFORE projection, period=5y → first
        in-horizon occurrence at +2y, NOT at +5y from anchor."""
        # Anchor 2023-05 (24 months BEFORE 2026-05), period=5y.
        # first_offset = -24
        # k=0: m=-24 (skip), k=1: m=36 (= 2y after PROJ_START? no, m=36 is 3y)
        # Wait: first_offset + k*60. k=0: -24, k=1: 36, k=2: 96.
        # 36 months = 3y from projection start; corresponds to 2029-05.
        # Anchor was 2023-05; +5y = 2028-05 (offset 24), but k=0 lands at -24.
        # k=1 lands at first_offset + 60 = 36 → 2029-05. That's 2y after the
        # 2028-05 "true" next occurrence — because period is 5y, the next
        # post-anchor occurrence is at anchor + 5y = 2028-05 = offset 24.
        # We MUST land at 24, not 36.  Let me re-anchor:
        # Anchor at (PROJ_START - 24 months) = 2024-05. Then k=0: m=-24, k=1: m=36.
        # If anchor at PROJ_START - 36 months = 2023-05, k=0: m=-36, k=1: m=24.
        # Spec says "Car bought 2024-Mar, period=5y → first in-horizon at 2029-Mar (k=1)".
        # Spec test #9: "anchor 3y BEFORE → first in-horizon at +2y, then +7y, +12y".
        # 3y = 36 months before; first_offset = -36. k=1: m=24 (2y after PROJ_START). PASSED.
        event = _recurring(
            date(2023, 5, 1),  # 36 months before PROJ_START
            -10_000.0,
            5,
        )
        out = apply_life_event_deltas(
            _flat_series(), [event], PROJ_START, HORIZON, FX
        )
        # first_offset = -36. Occurrences: -36, 24, 84, 144, 204, 264, 324, 384(past).
        # In-horizon ones: 24, 84, 144, 204, 264, 324.
        spike = +37_000.0
        expected = [24, 84, 144, 204, 264, 324]
        for m in expected:
            assert out[m] == pytest.approx(FLAT_EXPENSE + spike), f"offset {m}"
        # k=0 at -36 must be skipped (no negative-index write).
        for m in range(HORIZON):
            if m in expected:
                continue
            assert out[m] == pytest.approx(FLAT_EXPENSE), f"unexpected at {m}"

    def test_recurring_anchor_decades_before_projection_still_fires(self):
        """Codex BLOCKER regression — Spec D commit #2 review.

        An anchor MANY periods before projection_start (e.g. a legacy
        row migrated with anchor=1990 instead of today) must still
        produce the correct in-horizon occurrences, NOT be silently
        dropped by an iterations-budget safety net.

        Anchor 1990-05-01 (36*12 = 432 months before 2026-05-29),
        period=5y → first in-horizon at offset (1990 + k*5 >= 2026)
        with k=8 → 1990+40=2030 → offset 48.  Subsequent at 108, 168,
        228, 288, 348.
        """
        event = _recurring(date(1990, 5, 1), -10_000.0, 5)
        out = apply_life_event_deltas(
            _flat_series(), [event], PROJ_START, HORIZON, FX
        )
        # first_offset = -432.  Smallest k with first_offset + k*60 >= 0
        # is k = ceil(432/60) = 8.  m=8*60-432 = 48.
        spike = +37_000.0
        expected = [48, 108, 168, 228, 288, 348]
        for m in expected:
            assert out[m] == pytest.approx(FLAT_EXPENSE + spike), f"offset {m}"
        for m in range(HORIZON):
            if m in expected:
                continue
            assert out[m] == pytest.approx(FLAT_EXPENSE), f"unexpected at {m}"

    def test_recurring_centuries_before_projection_still_terminates(self):
        """Even more extreme: anchor 1700-01-01 (~3900 months before).
        Must terminate in O(horizon/period) iterations, not O(months
        between anchor and now)."""
        event = _recurring(date(1700, 1, 1), -1_000.0, 10)
        # If this hangs / OOMs the codex BLOCKER fix is wrong.
        out = apply_life_event_deltas(
            _flat_series(), [event], PROJ_START, HORIZON, FX
        )
        # The function should return a valid list; we don't pin the
        # exact offsets here — just confirm termination + that at least
        # SOME spike landed.
        assert len(out) == HORIZON
        n_modified = sum(1 for v in out if v != FLAT_EXPENSE)
        assert n_modified >= 1, "expected at least one in-horizon spike"
        assert n_modified <= 4, "30y/10y → at most 3 spikes plus boundary"

    def test_recurring_skips_invalid_period_zero(self):
        """Defensive — period <= 0 is rejected by DB CHECK; helper
        guards against malformed rows that bypass validation."""
        event = _recurring(date(2027, 3, 1), -10_000.0, 0)
        out = apply_life_event_deltas(
            _flat_series(), [event], PROJ_START, HORIZON, FX
        )
        assert out == _flat_series()

    def test_recurring_skips_fractional_period_codex_blocker_2(self):
        """Codex BLOCKER #2 regression — Spec D commit #2 re-review.

        A fractional 0 < period_years < 1 would, after ``int(...)``
        coercion, become period_months = 0 — causing division-by-zero
        in the first-valid-k computation OR an infinite loop where
        m_offset never advances.  Must be silently skipped as
        malformed.  (The ORM column is Integer; this can only happen
        via duck-typed fixtures or post-write data corruption.)
        """
        event = _recurring(date(2027, 3, 1), -10_000.0, 0.5)
        # If the fix is wrong, this either hangs or raises
        # ZeroDivisionError.
        out = apply_life_event_deltas(
            _flat_series(), [event], PROJ_START, HORIZON, FX
        )
        assert out == _flat_series()

    def test_recurring_skips_non_numeric_period(self):
        """Same BLOCKER #2 — a non-coercible period (string, object,
        etc.) must be silently skipped, not raise."""
        event = _recurring(date(2027, 3, 1), -10_000.0, "five")
        out = apply_life_event_deltas(
            _flat_series(), [event], PROJ_START, HORIZON, FX
        )
        assert out == _flat_series()


# ---------------------------------------------------------------------------
# Phase change tests — spec §7.1 cases 12-17, 19
# ---------------------------------------------------------------------------


class TestPhaseChange:
    def test_phase_change_start_open_ended(self):
        """Spec Appendix A: kids leave home 2034-08, +1500 USD/mo."""
        event = _phase_start(date(2034, 8, 15), +1_500.0)
        out = apply_life_event_deltas(
            _flat_series(), [event], PROJ_START, HORIZON, FX
        )
        # start_offset = (2034-08) - (2026-05) = 99
        # Per spec: series[99..359] = 30000 - 5550 = 24450
        # series[0..98] unchanged = 30000
        for m in range(99):
            assert out[m] == pytest.approx(FLAT_EXPENSE)
        for m in range(99, HORIZON):
            assert out[m] == pytest.approx(FLAT_EXPENSE - 5_550.0)

    def test_phase_change_start_at_exact_month_boundary_included(self):
        """§7.1 #17 — month of phase_start_date IS included."""
        event = _phase_start(date(2034, 8, 1), +1_500.0)
        out = apply_life_event_deltas(
            _flat_series(), [event], PROJ_START, HORIZON, FX
        )
        # Same offset 99 — August 2034 is INCLUDED.
        assert out[99] == pytest.approx(FLAT_EXPENSE - 5_550.0)
        assert out[98] == pytest.approx(FLAT_EXPENSE)

    def test_phase_change_start_before_projection_clamps_to_zero(self):
        """§7.1 #12 — phase already active at projection start."""
        event = _phase_start(date(2020, 1, 1), +1_500.0)
        out = apply_life_event_deltas(
            _flat_series(), [event], PROJ_START, HORIZON, FX
        )
        # Whole series modified from m=0.
        for m in range(HORIZON):
            assert out[m] == pytest.approx(FLAT_EXPENSE - 5_550.0)

    def test_phase_change_end_closed_band(self):
        """§7.1 #13 — phase_change_end applies inside band only."""
        # Phase from offset 24 (inclusive) to offset 48 (exclusive).
        # 2026-05 + 24m = 2028-05 ; 2026-05 + 48m = 2030-05.
        event = _phase_end(date(2028, 5, 1), date(2030, 5, 1), -2_000.0)
        out = apply_life_event_deltas(
            _flat_series(), [event], PROJ_START, HORIZON, FX
        )
        # Inside band [24, 48): expense increases by 2000*3.7=7400.
        for m in range(24, 48):
            assert out[m] == pytest.approx(FLAT_EXPENSE + 7_400.0)
        # Outside band: untouched.
        for m in list(range(0, 24)) + list(range(48, HORIZON)):
            assert out[m] == pytest.approx(FLAT_EXPENSE)

    def test_phase_change_end_excludes_end_month(self):
        """End is EXCLUSIVE — series[end_offset] is untouched."""
        # Band offset 24..48; series[48] should NOT be modified.
        event = _phase_end(date(2028, 5, 1), date(2030, 5, 1), -2_000.0)
        out = apply_life_event_deltas(
            _flat_series(), [event], PROJ_START, HORIZON, FX
        )
        assert out[47] == pytest.approx(FLAT_EXPENSE + 7_400.0)
        assert out[48] == pytest.approx(FLAT_EXPENSE)

    def test_phase_change_past_horizon_capped(self):
        """§7.1 #14 — phase extending past horizon capped at
        horizon_months."""
        event = _phase_end(date(2030, 1, 1), date(2099, 1, 1), -2_000.0)
        out = apply_life_event_deltas(
            _flat_series(), [event], PROJ_START, HORIZON, FX
        )
        start_offset = _months_between_dates(PROJ_START, date(2030, 1, 1))
        # All months from start_offset to HORIZON modified.
        for m in range(start_offset, HORIZON):
            assert out[m] == pytest.approx(FLAT_EXPENSE + 7_400.0)

    def test_phase_change_negative_amount_increases_expense(self):
        """§7.1 #19 — negative monthly_delta_usd INCREASES expense."""
        event = _phase_start(date(2030, 1, 1), -1_000.0)  # partner retires
        out = apply_life_event_deltas(
            _flat_series(), [event], PROJ_START, HORIZON, FX
        )
        start_offset = _months_between_dates(PROJ_START, date(2030, 1, 1))
        # series[m] += 1000 * 3.7 = 3700 from start onward
        for m in range(start_offset, HORIZON):
            assert out[m] == pytest.approx(FLAT_EXPENSE + 3_700.0)

    def test_phase_change_overlapping_additive(self):
        """§7.1 #15 — two overlapping phase changes apply additively."""
        # Two "dependent_leaves" rows at different times.
        # PROJ_START = 2026-05. _months_between(2026-05, 2030-01) = 44.
        # _months_between(2026-05, 2032-01) = 68.
        e1 = _phase_start(date(2030, 1, 1), +1_000.0)  # offset 44
        e2 = _phase_start(date(2032, 1, 1), +500.0)  # offset 68
        out = apply_life_event_deltas(
            _flat_series(), [e1, e2], PROJ_START, HORIZON, FX
        )
        # 0..43: unchanged
        assert out[0] == pytest.approx(FLAT_EXPENSE)
        assert out[43] == pytest.approx(FLAT_EXPENSE)
        # 44..67: e1 only → -3700
        for m in range(44, 68):
            assert out[m] == pytest.approx(FLAT_EXPENSE - 3_700.0)
        # 68..end: e1 + e2 → -3700 - 1850 = -5550
        for m in range(68, HORIZON):
            assert out[m] == pytest.approx(FLAT_EXPENSE - 5_550.0)


# ---------------------------------------------------------------------------
# delta_kind='none' — spec §7.1 #20 + §7.6 #1, #2
# ---------------------------------------------------------------------------


class TestNoneKind:
    def test_none_event_alone_no_effect(self):
        """§7.6 #1 — series byte-identical when all events are none."""
        base = _flat_series()
        out = apply_life_event_deltas(
            base,
            [_none_event(), _none_event()],
            PROJ_START,
            HORIZON,
            FX,
        )
        assert out == base

    def test_none_plus_one_shot_only_one_shot_applies(self):
        """§7.6 #2 — mixed none + one_shot = same as just one_shot."""
        oneshot = _one_shot(date(2030, 1, 1), -10_000.0)
        out_mixed = apply_life_event_deltas(
            _flat_series(),
            [_none_event(), oneshot, _none_event()],
            PROJ_START,
            HORIZON,
            FX,
        )
        out_one = apply_life_event_deltas(
            _flat_series(), [oneshot], PROJ_START, HORIZON, FX
        )
        assert out_mixed == out_one


# ---------------------------------------------------------------------------
# Mixed events
# ---------------------------------------------------------------------------


class TestMixed:
    def test_one_shot_plus_phase_change_same_month_additive(self):
        """§7.1 #16 — one_shot + phase_change at same month: additive."""
        # PROJ_START = 2026-05. _months_between(2026-05, 2030-01) = 44.
        # phase_start at 2030-01 (offset 44), +1000 USD/mo
        # one_shot at 2030-01-15 (offset 44), -5000 USD
        ph = _phase_start(date(2030, 1, 1), +1_000.0)
        os = _one_shot(date(2030, 1, 15), -5_000.0)
        out = apply_life_event_deltas(
            _flat_series(), [ph, os], PROJ_START, HORIZON, FX
        )
        # series[44] = 30000 + (-3700 from phase) + (+18500 from one_shot)
        #            = 30000 - 3700 + 18500 = 44800
        assert out[44] == pytest.approx(44_800.0)
        # series[45] = phase only = 30000 - 3700 = 26300
        assert out[45] == pytest.approx(26_300.0)

    def test_all_three_shapes_apply(self):
        """End-to-end: one_shot + recurring + phase_change."""
        os = _one_shot(date(2028, 1, 15), -50_000.0)  # offset 20
        rec = _recurring(date(2027, 3, 15), -10_000.0, 5)  # offsets 10, 70, ...
        ph = _phase_start(date(2034, 1, 1), +500.0)  # offset 92
        out = apply_life_event_deltas(
            _flat_series(), [os, rec, ph], PROJ_START, HORIZON, FX
        )
        # offset 10: recurring only → +37000
        assert out[10] == pytest.approx(FLAT_EXPENSE + 37_000.0)
        # offset 20: one_shot only → +185000
        assert out[20] == pytest.approx(FLAT_EXPENSE + 185_000.0)
        # offset 70: recurring only → +37000
        assert out[70] == pytest.approx(FLAT_EXPENSE + 37_000.0)
        # offset 92: phase only → -1850
        assert out[92] == pytest.approx(FLAT_EXPENSE - 1_850.0)
        # offset 130: recurring + phase → +37000 - 1850
        assert out[130] == pytest.approx(FLAT_EXPENSE + 37_000.0 - 1_850.0)
        # offset 95: phase only → -1850
        assert out[95] == pytest.approx(FLAT_EXPENSE - 1_850.0)


# ---------------------------------------------------------------------------
# FX degeneracy — spec §7.1 #21
# ---------------------------------------------------------------------------


class TestFxDegenerate:
    def test_fx_zero_no_change(self):
        """§7.1 #21 — FX=0 produces zero contribution from any event."""
        events = [
            _one_shot(date(2030, 1, 1), -50_000.0),
            _recurring(date(2027, 3, 1), -10_000.0, 5),
            _phase_start(date(2030, 1, 1), +1_000.0),
        ]
        out = apply_life_event_deltas(
            _flat_series(),
            events,
            PROJ_START,
            HORIZON,
            fx_usd_nis_for_event=0.0,
        )
        assert out == _flat_series()


# ---------------------------------------------------------------------------
# Worked examples from spec Appendix
# ---------------------------------------------------------------------------


class TestSpecWorkedExamples:
    """End-to-end reproduction of spec Appendix A-E."""

    def test_example_a_kids_leave_home(self):
        """Appendix A — phase change drops series by 5550 NIS from
        offset 99 onward."""
        event = _phase_start(date(2034, 8, 15), +1_500.0)
        out = apply_life_event_deltas(
            _flat_series(), [event], PROJ_START, HORIZON, FX
        )
        assert out[98] == pytest.approx(30_000.0)
        assert out[99] == pytest.approx(24_450.0)
        assert out[359] == pytest.approx(24_450.0)

    def test_example_c_inheritance_plus_wedding(self):
        """Appendix C — $200K inheritance 2028-11 + $50K wedding 2031-06."""
        events = [
            _one_shot(date(2031, 6, 10), -50_000.0),  # wedding out
            _one_shot(date(2028, 11, 20), +200_000.0),  # inheritance in
        ]
        out = apply_life_event_deltas(
            _flat_series(), events, PROJ_START, HORIZON, FX
        )
        # series[30] = 30000 - 740000 = -710000
        assert out[30] == pytest.approx(-710_000.0)
        # series[61] = 30000 + 185000 = 215000
        assert out[61] == pytest.approx(215_000.0)

    def test_example_e_sigma_calibration_none(self):
        """Appendix E — delta_kind='none' returns unchanged series."""
        event = SimpleNamespace(
            delta_kind="none",
            target_date=None,
            phase_start_date=None,
            phase_end_date=None,
            monthly_delta_usd=None,
            one_shot_amount_usd=None,
            recurring_amount_usd=None,
            recurring_period_years=None,
        )
        out = apply_life_event_deltas(
            _flat_series(), [event], PROJ_START, HORIZON, FX
        )
        assert out == _flat_series()


# ---------------------------------------------------------------------------
# Determinism — pure function contract
# ---------------------------------------------------------------------------


class TestPurity:
    def test_same_inputs_same_output(self):
        events = [
            _one_shot(date(2030, 1, 1), -50_000.0),
            _recurring(date(2027, 3, 1), -10_000.0, 5),
            _phase_start(date(2034, 1, 1), +500.0),
        ]
        out1 = apply_life_event_deltas(
            _flat_series(), events, PROJ_START, HORIZON, FX
        )
        out2 = apply_life_event_deltas(
            _flat_series(), events, PROJ_START, HORIZON, FX
        )
        assert out1 == out2

    def test_no_db_or_session_dependency(self):
        """The function signature does not accept a session; just
        invoking it with pure-Python args must succeed."""
        # If this test runs at all, the function ran without DB access.
        out = apply_life_event_deltas(
            _flat_series(),
            [_one_shot(date(2030, 1, 1), -1.0)],
            PROJ_START,
            HORIZON,
            FX,
        )
        assert isinstance(out, list)
        assert len(out) == HORIZON
