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


class TestHistoricalSaleArithmetic:
    """Historical arithmetic fixture, not proof of tax-component allocation.

    One illustrative FX is used here; final filing requires sale-date FX and
    certificates. Proceeds-minus-wire may also include trustee/wire expenses.
    """

    FX = 2.9910
    LOTS = [   # (grant benchmark, shares, sale price)
        (18.3305, 560, 191.3301), (18.3305, 520, 176.59), (18.3305, 1040, 199.5601),
        (18.3305, 560, 216.085), (18.3305, 700, 219.93), (18.1159, 560, 223.80),
    ]
    PROCEEDS_WIRE_GAP_USD = 217_766.76  # after broker commissions, not certified tax

    def test_capital_estimate_and_illustrative_gap_comparison(self):
        gain = sum(s102.capital_gain_usd(sh, px, b) for b, sh, px in self.LOTS)
        assert sum(sh for _, sh, _ in self.LOTS) == 3940
        assert gain == pytest.approx(734_698.766, abs=0.0001)
        tax = s102.capital_tax_ils(gain * self.FX)
        gap_ils = self.PROCEEDS_WIRE_GAP_USD * self.FX
        assert tax.tax_ils == pytest.approx(gap_ils, rel=0.02)

    def test_the_effective_rate_sits_in_the_surtax_band(self):
        gain = sum(s102.capital_gain_usd(sh, px, b) for b, sh, px in self.LOTS)
        r = s102.capital_tax_ils(gain * self.FX).effective_rate
        assert 0.28 < r < 0.30

    def test_a_broker_basis_would_break_the_agreement(self):
        """Different basis gives a different ratio; this does not prove a tax rule."""
        schwab_gain_usd = 473_169.36
        implied = self.PROCEEDS_WIRE_GAP_USD / schwab_gain_usd
        assert implied > 0.42, f"implied {implied:.1%} — nowhere near 28-30%"


class TestOrdinarySlice:
    """The slice that was missing, and that moved the bill 45%."""

    def test_it_is_shares_times_benchmark_not_sale_price(self):
        assert s102.ordinary_income_usd(1000, 18.3305) == pytest.approx(18_330.5)

    def test_it_does_not_move_with_the_sale_price(self):
        """Timing-invariant: this is why pacing the glide saves so little."""
        a = s102.ordinary_income_usd(1000, 18.3305)
        assert a == s102.ordinary_income_usd(1000, 18.3305)

    def test_the_rate_is_fifty_percent(self):
        assert s102.ordinary_tax_ils(100_000.0) == pytest.approx(50_000.0)

    def test_negative_inputs_are_rejected(self):
        with pytest.raises(ValueError, match="non-negative"):
            s102.ordinary_income_usd(-1, 18.0)
        with pytest.raises(ValueError, match="non-negative"):
            s102.ordinary_tax_ils(-1.0)


class TestSaleTaxAgainstForm106:
    """Both slices enter the settled model; a wire gap alone cannot allocate tax."""

    FX = 2.9910

    def test_both_slices_are_charged_on_one_sale(self):
        t = s102.sale_tax(1000, 200.0, 18.3305, fx=self.FX)
        assert t.capital.tax_ils > 0
        assert t.ordinary_tax_ils > 0
        assert t.total_tax_ils == pytest.approx(
            t.capital.tax_ils + t.ordinary_tax_ils)

    def test_a_higher_benchmark_costs_MORE_total_tax(self):
        """The inversion that cost two pieces of advice: retain the HIGHEST
        benchmark, sell the lowest."""
        low = s102.sale_tax(1000, 200.0, 18.3305, fx=self.FX)
        high = s102.sale_tax(1000, 200.0, 87.4976, fx=self.FX)
        assert high.total_tax_ils > low.total_tax_ils

    def test_form_106_proportions_reproduce(self):
        """Capital + ordinary = 97.2% of gross 102 proceeds on the 2025 form
        (ILS 1,327,411 + 411,704 against 1,790,099); gap is not proof of fees."""
        assert (1_327_411 + 411_704) / 1_790_099 == pytest.approx(0.972, abs=0.001)

    def test_net_retention_is_68_not_73_percent(self):
        """Illustrative gross liability, not additional unpaid household tax.

        Actual credited withholding must be reconciled before reserve changes.
        """
        lots = TestHistoricalSaleArithmetic.LOTS
        gross = sum(sh * px for _, sh, px in lots) * self.FX
        gain = sum(s102.capital_gain_usd(sh, px, b) for b, sh, px in lots) * self.FX
        ordinary = sum(s102.ordinary_income_usd(sh, b) for b, sh, _ in lots) * self.FX
        total = s102.capital_tax_ils(gain).tax_ils + s102.ordinary_tax_ils(ordinary)
        retention = (gross - total) / gross
        assert retention == pytest.approx(0.68, abs=0.01), f"got {retention:.1%}"
