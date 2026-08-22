"""Section 102 arithmetic, pinned against the trustee engine and real cash."""

from __future__ import annotations

from datetime import date

import pytest

from argosy.services import section_102 as s102


class TestGrantBenchmark:
    """The 30-trading-day mean reproduces NVIDIA's trustee engine exactly."""

    @pytest.fixture(scope="class")
    def closes(self):
        yf = pytest.importorskip("yfinance")
        h = yf.Ticker("NVDA").history(
            start="2021-04-01", end="2026-08-23", auto_adjust=False)
        if h.empty:
            pytest.skip("no network / yfinance data")
        c = h["Close"]
        c.index = c.index.tz_localize(None)
        return c

    @pytest.mark.parametrize(
        "grant_date,trustee_px",
        [
            (date(2022, 6, 8), 18.1159),
            (date(2023, 6, 8), 31.9859),
            (date(2024, 4, 8), 87.4976),
        ],
    )
    def test_reproduces_the_trustee(self, closes, grant_date, trustee_px):
        got = s102.grant_benchmark(closes, grant_date)
        assert got == pytest.approx(trustee_px, rel=1e-4)

    def test_it_is_the_mean_not_the_fmv_at_grant(self, closes):
        """Grant 246477 settles it beyond coincidence: FMV at grant was 38.4351
        while the trustee used 31.9859 — a 17% gap."""
        got = s102.grant_benchmark(closes, date(2023, 6, 8))
        assert got == pytest.approx(31.9859, rel=1e-4)
        assert abs(got - 38.4351) > 5.0

    def test_raises_rather_than_averaging_a_short_window(self, closes):
        with pytest.raises(ValueError, match="need 31 closes"):
            s102.grant_benchmark(closes, date(2021, 4, 2))


class TestCapitalGain:
    def test_uses_the_grant_benchmark_not_a_broker_basis(self):
        # 280 sh of grant 213000 sold at 223.80. Schwab reports basis 17.674
        # (vest FMV) and a gain of 57,715. The Israeli figure differs.
        israeli = s102.capital_gain_usd(280, 223.80, 18.1159)
        schwab = 280 * (223.80 - 17.674)
        assert israeli == pytest.approx(57_591.5, abs=1.0)
        assert schwab == pytest.approx(57_715.3, abs=1.0)
        assert israeli != pytest.approx(schwab, abs=1.0)

    def test_the_8x_case(self):
        """Grant 182406's real benchmark is 18.3305; its vest FMV is ~149.38.
        Proxying the broker basis understates that grant's gain 8-fold."""
        real = s102.capital_gain_usd(1420, 215.38, 18.3305)
        proxy = s102.capital_gain_usd(1420, 215.38, 149.38)
        assert real / proxy > 2.9
        assert real == pytest.approx(279_810, abs=50)


class TestCapitalTaxBands:
    def test_first_shekels_are_28_percent(self):
        b = s102.capital_tax_ils(100_000.0)
        assert b.at_30_ils == 0.0
        assert b.tax_ils == pytest.approx(28_000.0)
        assert b.effective_rate == pytest.approx(0.28)

    def test_the_capital_layer_bites_only_above_the_threshold(self):
        b = s102.capital_tax_ils(1_000_000.0)
        assert b.at_28_ils == pytest.approx(s102.SURTAX_THRESHOLD_ILS)
        assert b.at_30_ils == pytest.approx(1_000_000.0 - s102.SURTAX_THRESHOLD_ILS)
        assert b.tax_ils == pytest.approx(
            s102.SURTAX_THRESHOLD_ILS * 0.28 + (1_000_000.0 - s102.SURTAX_THRESHOLD_ILS) * 0.30)

    def test_prior_capital_income_consumes_the_headroom(self):
        b = s102.capital_tax_ils(500_000.0, prior_capital_income_ils=s102.SURTAX_THRESHOLD_ILS)
        assert b.at_28_ils == 0.0
        assert b.effective_rate == pytest.approx(0.30)

    def test_salary_below_threshold_drops_the_general_layer(self):
        b = s102.capital_tax_ils(100_000.0, salary_clears_threshold=False)
        assert b.effective_rate == pytest.approx(0.25)

    def test_negative_gain_is_rejected(self):
        with pytest.raises(ValueError, match="non-negative"):
            s102.capital_tax_ils(-1.0)


class TestAgainstTheRealWithholding:
    """The 2026 sales are the strongest available check: two independent
    computations — statutory tax, and cash actually withheld — must agree."""

    FX = 2.9910
    LOTS = [   # (grant benchmark, shares, sale price)
        (18.3305, 560, 191.33), (18.3305, 517, 176.59), (18.3305, 1040, 199.56),
        (18.3305, 560, 216.09), (18.3305, 700, 219.93), (18.1159, 560, 223.80),
    ]
    WITHHELD_USD = 217_766.76   # gross 806,787.47 minus wires 589,020.71

    def test_computed_tax_matches_cash_withheld_within_two_percent(self):
        gain = sum(s102.capital_gain_usd(sh, px, b) for b, sh, px in self.LOTS)
        assert gain == pytest.approx(734_227, abs=500)
        tax = s102.capital_tax_ils(gain * self.FX)
        withheld_ils = self.WITHHELD_USD * self.FX
        assert tax.tax_ils == pytest.approx(withheld_ils, rel=0.02)

    def test_the_effective_rate_sits_in_the_surtax_band(self):
        gain = sum(s102.capital_gain_usd(sh, px, b) for b, sh, px in self.LOTS)
        r = s102.capital_tax_ils(gain * self.FX).effective_rate
        assert 0.28 < r < 0.30

    def test_a_broker_basis_would_break_the_agreement(self):
        """Sanity: if the vest-FMV basis were correct, the withheld cash would
        imply a rate far outside the statutory band. It doesn't — which is what
        confirms the grant-benchmark reading."""
        schwab_gain_usd = 472_910.71
        implied = self.WITHHELD_USD / schwab_gain_usd
        assert implied > 0.42, f"implied {implied:.1%} — nowhere near 28-30%"
