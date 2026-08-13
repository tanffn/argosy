"""Tests for GET /api/retirement/projection/deconcentration-optimizer.

Covers:
  - happy path: 200 with well-formed per-horizon table + chosen horizon
  - degraded path: FI-spend basis unavailable → 404
  - pure-function invariants: effective_cgt_rate + total_cgt_for_horizon tax math
    (no DB, no MC)
"""
from __future__ import annotations

import pytest

from argosy.services.retirement.deconcentration_optimizer import (
    CGT_BASE_RATE,
    CGT_MARGINAL_ABOVE_THRESHOLD,
    DEFAULT_HORIZONS,
    NVDA_TAXABLE_GAIN_FRACTION,
    SURTAX_THRESHOLD_NIS,
    DeconcentrationPlan,
    HorizonOutcome,
    effective_cgt_rate,
    total_cgt_for_horizon,
)


# ---------------------------------------------------------------------------
# Pure-function unit tests — no DB, no MC, no monkeypatch
# ---------------------------------------------------------------------------


class TestEffectiveCgtRate:
    """effective_cgt_rate returns [0.28, 0.30] for positive gains."""

    def test_zero_gain_returns_base_rate(self):
        assert effective_cgt_rate(0.0) == pytest.approx(CGT_BASE_RATE)

    def test_negative_gain_returns_base_rate(self):
        assert effective_cgt_rate(-100_000.0) == pytest.approx(CGT_BASE_RATE)

    def test_small_gain_below_threshold_returns_28_pct(self):
        # Below the surtax threshold: 25% + 3% mas-yesef on the whole gain,
        # 2% capital-source levy only on the slice above threshold → 0 here.
        gain = 100_000.0
        rate = effective_cgt_rate(gain)
        assert rate == pytest.approx(0.28, abs=1e-9)

    def test_large_gain_above_threshold_approaches_marginal(self):
        # Very large gain: almost all above threshold → approaches 30%.
        gain = 100_000_000.0
        rate = effective_cgt_rate(gain)
        assert 0.299 < rate <= CGT_MARGINAL_ABOVE_THRESHOLD

    def test_rate_monotone_increasing_with_gain(self):
        gains = [50_000.0, 500_000.0, 2_000_000.0, 10_000_000.0]
        rates = [effective_cgt_rate(g) for g in gains]
        for lo, hi in zip(rates, rates[1:]):
            assert hi >= lo - 1e-9


class TestTotalCgtForHorizon:
    """total_cgt_for_horizon: longer horizons pay less tax (bunching penalty)."""

    TAXABLE_GAIN = 3_420_000.0  # ₪3.42M — the scenario from the docstring

    def test_h1_returns_positive_cgt_and_rate(self):
        total, rate = total_cgt_for_horizon(self.TAXABLE_GAIN, 1)
        assert total > 0
        assert 0.28 <= rate <= 0.30

    def test_longer_horizon_less_total_cgt(self):
        """Spreading over more years reduces bunching → lower total PV tax."""
        cgts = [total_cgt_for_horizon(self.TAXABLE_GAIN, h)[0] for h in range(1, 6)]
        for earlier, later in zip(cgts, cgts[1:]):
            assert later < earlier

    def test_h1_rate_equals_single_year_rate(self):
        total, rate = total_cgt_for_horizon(self.TAXABLE_GAIN, 1)
        expected_rate = effective_cgt_rate(self.TAXABLE_GAIN)
        assert rate == pytest.approx(expected_rate)
        assert total == pytest.approx(rate * self.TAXABLE_GAIN)

    def test_zero_gain_returns_zero_tax(self):
        total, rate = total_cgt_for_horizon(0.0, 3)
        assert total == pytest.approx(0.0)

    def test_pv_discount_reduces_multi_year_total(self):
        """PV discount at 2% real: H=2 total < H=1 total per year × 2."""
        gain = 2_000_000.0
        h1_total, h1_rate = total_cgt_for_horizon(gain, 1)
        h2_total, _ = total_cgt_for_horizon(gain, 2)
        # Each year's tax is smaller (lower rate) AND discounted; must be < h1.
        assert h2_total < h1_total


# ---------------------------------------------------------------------------
# Route integration tests — monkeypatched optimizer (avoids full MC cost)
# ---------------------------------------------------------------------------


def _fake_plan() -> DeconcentrationPlan:
    """A synthetic DeconcentrationPlan with plausible values for route testing."""
    rows = [
        HorizonOutcome(
            horizon=h,
            total_cgt_nis=900_000.0 - h * 30_000.0,
            eff_cgt_rate=0.295 - h * 0.003,
            drawdown_age=52 - h,
            deployable_nis=9_000_000.0 + h * 30_000.0,
            sigma_path_desc=f"σ 34%→18% over {h}y",
        )
        for h in range(1, 6)
    ]
    best = min(rows, key=lambda r: (r.drawdown_age, r.total_cgt_nis))
    return DeconcentrationPlan(
        chosen_horizon_years=best.horizon,
        per_horizon=rows,
        full_portfolio_nis=11_000_000.0,
        reserve_pv_nis=1_300_000.0,
        total_taxable_gain_nis=3_420_000.0,
        nvda_current_pct=0.65,
        nvda_cap_pct=0.13,
        sell_nis=5_720_000.0,
        sigma_current=0.344,
        assumptions={"source": "test"},
    )


class TestDeconcentrationOptimizerRoute:
    """Route-level tests for GET /api/retirement/projection/deconcentration-optimizer."""

    def test_happy_path_returns_200_with_per_horizon_table(
        self, client_with_db, monkeypatch
    ):
        """Happy path: optimizer returns a valid plan → 200 with all fields."""
        from argosy.api.routes import retirement as retirement_mod

        monkeypatch.setattr(
            retirement_mod, "optimize_deconcentration", lambda **_kw: _fake_plan()
        )
        r = client_with_db.get(
            "/api/retirement/projection/deconcentration-optimizer?user_id=ariel"
        )
        assert r.status_code == 200
        body = r.json()

        # Top-level fields present
        assert "chosen_horizon_years" in body
        assert "per_horizon" in body
        assert "full_portfolio_nis" in body
        assert "reserve_pv_nis" in body
        assert "total_taxable_gain_nis" in body
        assert "sell_nis" in body
        assert "sigma_current" in body
        assert "assumptions" in body

        # All 5 horizons returned
        assert len(body["per_horizon"]) == len(DEFAULT_HORIZONS)

        # Each horizon row has the expected keys
        for row in body["per_horizon"]:
            assert "horizon" in row
            assert "total_cgt_nis" in row
            assert "eff_cgt_rate" in row
            assert "drawdown_age" in row
            assert "deployable_nis" in row
            assert "sigma_path_desc" in row

        # Chosen horizon is one of the returned horizons
        assert body["chosen_horizon_years"] in [r["horizon"] for r in body["per_horizon"]]

    def test_happy_path_chosen_horizon_is_best_drawdown_age(
        self, client_with_db, monkeypatch
    ):
        """The chosen horizon minimises drawdown age (tie-break: lower CGT)."""
        from argosy.api.routes import retirement as retirement_mod

        plan = _fake_plan()
        monkeypatch.setattr(
            retirement_mod, "optimize_deconcentration", lambda **_kw: plan
        )
        r = client_with_db.get(
            "/api/retirement/projection/deconcentration-optimizer?user_id=ariel"
        )
        body = r.json()
        chosen = body["chosen_horizon_years"]
        min_age = min(
            row["drawdown_age"] for row in body["per_horizon"]
            if row["drawdown_age"] is not None
        )
        chosen_row = next(
            row for row in body["per_horizon"] if row["horizon"] == chosen
        )
        assert chosen_row["drawdown_age"] == min_age

    def test_degraded_path_returns_404_on_value_error(
        self, client_with_db, monkeypatch
    ):
        """When the optimizer raises ValueError (no FI basis), route returns 404."""
        from argosy.api.routes import retirement as retirement_mod

        def _raise(**_kw):
            raise ValueError(
                "scenario MC needs a permanent-equivalent spend basis "
                "(fi_methodology returned no baseline) — refusing to fabricate one."
            )

        monkeypatch.setattr(
            retirement_mod, "optimize_deconcentration", _raise
        )
        r = client_with_db.get(
            "/api/retirement/projection/deconcentration-optimizer?user_id=ariel"
        )
        assert r.status_code == 404
        assert "refusing to fabricate" in r.json()["detail"]

    def test_nvda_already_at_cap_sell_zero(self, client_with_db, monkeypatch):
        """When NVDA is already at cap, sell_nis=0 and per-horizon CGT should be 0."""
        from argosy.api.routes import retirement as retirement_mod

        rows = [
            HorizonOutcome(
                horizon=h,
                total_cgt_nis=0.0,
                eff_cgt_rate=CGT_BASE_RATE,
                drawdown_age=49,
                deployable_nis=9_500_000.0,
                sigma_path_desc=f"σ 18%→18% over {h}y",
            )
            for h in range(1, 6)
        ]
        no_sale_plan = DeconcentrationPlan(
            chosen_horizon_years=1,
            per_horizon=rows,
            full_portfolio_nis=9_500_000.0,
            reserve_pv_nis=1_300_000.0,
            total_taxable_gain_nis=0.0,
            nvda_current_pct=0.10,
            nvda_cap_pct=0.13,
            sell_nis=0.0,
            sigma_current=0.18,
            assumptions={"source": "test_no_sale"},
        )
        monkeypatch.setattr(
            retirement_mod, "optimize_deconcentration", lambda **_kw: no_sale_plan
        )
        r = client_with_db.get(
            "/api/retirement/projection/deconcentration-optimizer?user_id=ariel"
        )
        assert r.status_code == 200
        body = r.json()
        assert body["sell_nis"] == pytest.approx(0.0)
        assert body["total_taxable_gain_nis"] == pytest.approx(0.0)
        for row in body["per_horizon"]:
            assert row["total_cgt_nis"] == pytest.approx(0.0)
