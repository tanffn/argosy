"""Section 102 capital-track arithmetic for Israeli employee equity.

Everything here is deterministic tax arithmetic — the inviolable-arithmetic
floor. It computes what a sale costs; it never judges whether to sell.

Three facts this module exists to encode, each of which cost real effort to
establish and each of which an agent got wrong first (see
``domain_knowledge/tax/israel/section_102.md``):

1. **The grant benchmark is the 30-trading-day MEAN of split-adjusted closes
   preceding the grant date** — not the FMV at grant, and emphatically not the
   broker's cost basis. Reproduces NVIDIA's trustee engine to 0.000% on all
   three grants it covers.
2. **The broker's basis is the WRONG basis.** Schwab reports the vest FMV (the
   US-tax number). Using it understated one real grant's gain by 8x.
3. **The capital tax is withheld by the TRUSTEE, in the wire leg** — invisible
   in the broker export, visible only as gross-minus-wire in the bank
   statement. Concluding "unwithheld" from broker fields alone produced a
   phantom six-figure liability.
4. **BOTH slices fall due at SALE, not at vest.** Across 74 ``Lapse`` events in
   the full-history Schwab export every tax field is empty and each ``Deposit``
   matches its ``Lapse`` quantity exactly — nothing is withheld at vesting. The
   2025 Form 106 confirms it from the other side, reporting the ordinary slice
   (ILS 411,704) as EMPLOYMENT income in the year of SALE alongside the capital
   slice (ILS 1,327,411). A portal election reading "Withhold Shares" is a
   preference, not evidence; assuming otherwise inverted two pieces of advice.

So the tax on one share is::

    0.30 x (S - B) + 0.50 x B   =   0.30 x S + 0.20 x B

for sale price ``S`` and grant benchmark ``B`` (0.30 being the capital
marginal above the surtax threshold — the banded computation lives in
:func:`capital_tax_ils`). A HIGHER benchmark therefore means HIGHER total tax:
it swaps 30%-taxed capital income for 50%-taxed ordinary income. Sell the
LOWEST-benchmark lots first, and retain the highest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

__all__ = [
    "SURTAX_THRESHOLD_ILS",
    "STATUTORY_CGT",
    "GENERAL_SURTAX",
    "CAPITAL_SOURCE_SURTAX",
    "ORDINARY_RATE",
    "TaxBands",
    "SaleTax",
    "grant_benchmark",
    "capital_gain_usd",
    "capital_tax_ils",
    "ordinary_income_usd",
    "ordinary_tax_ils",
    "sale_tax",
    "KNOWN_NVDA_BENCHMARKS",
]

#: Annual threshold above which the surtax layers bite (ILS). Indexed to wage
#: growth — re-verify each January against domain_knowledge/tax/israel/surtax.md.
SURTAX_THRESHOLD_ILS = 721_560.0

STATUTORY_CGT = 0.25
#: On TOTAL income above the threshold. A salary that already clears the
#: threshold means every shekel of capital gain carries this layer.
GENERAL_SURTAX = 0.03
#: Additional, and tests the CAPITAL-SOURCE income alone against the threshold.
CAPITAL_SOURCE_SURTAX = 0.02
#: The ordinary slice is EMPLOYMENT income (Form 106, 2025), so it carries the
#: top marginal 47% plus the 3% general surtax for a salary already past the
#: threshold — see domain_knowledge/tax/israel/surtax.md. Unlike the capital
#: slice this is flat and, crucially, **timing-invariant**: shares x benchmark
#: is fixed the day the grant is priced, so pacing sales across more tax years
#: cannot shrink it. Only the capital slice's 2% band responds to pacing, which
#: is why the premium for a fast glide is small.
ORDINARY_RATE = 0.50

#: Trustee-confirmed benchmarks, exact. Use these rather than recomputing when
#: the grant is one of them — they are the ground truth the derivation was
#: validated against.
KNOWN_NVDA_BENCHMARKS: dict[str, float] = {
    "213000": 18.1159,   # granted 2022-06-08
    "246477": 31.9859,   # granted 2023-06-08
    "289172": 87.4976,   # granted 2024-04-08
    "289173": 87.4976,   # granted 2024-04-08
}


@dataclass(frozen=True)
class TaxBands:
    """A capital-gain tax computation, split into the bands that produced it."""

    gain_ils: float
    prior_capital_income_ils: float
    at_28_ils: float          # 25% statutory + 3% general surtax
    at_30_ils: float          # + the 2% capital-source layer
    tax_ils: float

    @property
    def effective_rate(self) -> float:
        return self.tax_ils / self.gain_ils if self.gain_ils else 0.0


def grant_benchmark(
    closes,
    grant_date: date,
    *,
    window: int = 30,
) -> float:
    """Section-102 grant benchmark: the mean of the ``window`` trading-day
    closes STRICTLY PRECEDING ``grant_date``.

    ``closes`` is a pandas Series of split-adjusted closes indexed by date
    (``yfinance(..., auto_adjust=False)["Close"]`` is already split-adjusted;
    do NOT divide by the split ratio again — doing so once cost an hour).

    The grant-date close itself is excluded: including it shifts the NVIDIA
    grants off the trustee's figures, and excluding it reproduces all three
    to 0.000%.
    """
    import pandas as pd

    prior = closes[closes.index <= pd.Timestamp(grant_date)]
    if len(prior) < window + 1:
        raise ValueError(
            f"need {window + 1} closes on or before {grant_date}, have {len(prior)}"
        )
    return float(prior.iloc[:-1].tail(window).mean())


def capital_gain_usd(shares: float, sale_price: float, benchmark: float) -> float:
    """The Section-102 capital slice: ``shares x (sale price - grant benchmark)``.

    This is only HALF the bill. The ordinary slice (:func:`ordinary_income_usd`)
    falls due on the same sale — see fact 4 in the module docstring. An earlier
    revision of this docstring claimed the ordinary slice was "settled at VEST
    via sell-to-cover"; 74 empty-tax ``Lapse`` rows and the 2025 Form 106 both
    disprove that. Use :func:`sale_tax` unless you specifically want one slice.
    """
    return shares * (sale_price - benchmark)


def capital_tax_ils(
    gain_ils: float,
    *,
    prior_capital_income_ils: float = 0.0,
    salary_clears_threshold: bool = True,
) -> TaxBands:
    """Israeli tax on a capital gain, given capital income already realised
    this tax year.

    The two surtax layers test different things, which is the whole reason
    pacing sales across years is worth anything:

    * the 3% general layer tests TOTAL income — a salary above the threshold
      means it applies to the first shekel of gain;
    * the 2% capital-source layer tests CAPITAL income alone, so only the
      portion of the year's capital income above the threshold carries it.

    With ``salary_clears_threshold=False`` the general layer is not applied —
    the caller is asserting total income stays under the threshold, which for
    this household it does not.
    """
    if gain_ils < 0:
        raise ValueError(f"gain_ils must be non-negative, got {gain_ils}")
    headroom = max(0.0, SURTAX_THRESHOLD_ILS - max(0.0, prior_capital_income_ils))
    at_28 = min(gain_ils, headroom)
    at_30 = gain_ils - at_28
    base = STATUTORY_CGT + (GENERAL_SURTAX if salary_clears_threshold else 0.0)
    tax = at_28 * base + at_30 * (base + CAPITAL_SOURCE_SURTAX)
    return TaxBands(
        gain_ils=gain_ils, prior_capital_income_ils=prior_capital_income_ils,
        at_28_ils=round(at_28, 2), at_30_ils=round(at_30, 2), tax_ils=round(tax, 2),
    )


def ordinary_income_usd(shares: float, benchmark: float) -> float:
    """The Section-102 ordinary slice: ``shares x grant benchmark``.

    Independent of the sale price — the benchmark fixes it when the grant is
    priced. Reported as EMPLOYMENT income in the year of SALE (Form 106).
    """
    if shares < 0 or benchmark < 0:
        raise ValueError(f"shares and benchmark must be non-negative, got {shares}, {benchmark}")
    return shares * benchmark


def ordinary_tax_ils(ordinary_income_ils: float) -> float:
    """Tax on the ordinary slice: flat :data:`ORDINARY_RATE` for this household.

    No banding. The salary already clears the threshold, so there is no
    lower-rate headroom to allocate and nothing here responds to pacing.
    """
    if ordinary_income_ils < 0:
        raise ValueError(f"ordinary_income_ils must be non-negative, got {ordinary_income_ils}")
    return round(ordinary_income_ils * ORDINARY_RATE, 2)


@dataclass(frozen=True)
class SaleTax:
    """Both slices of one Section-102 sale, in ILS, plus what survives it."""

    gross_proceeds_ils: float
    capital: TaxBands
    ordinary_income_ils: float
    ordinary_tax_ils: float

    @property
    def total_tax_ils(self) -> float:
        return round(self.capital.tax_ils + self.ordinary_tax_ils, 2)

    @property
    def net_proceeds_ils(self) -> float:
        return round(self.gross_proceeds_ils - self.total_tax_ils, 2)

    @property
    def net_retention(self) -> float:
        """Fraction of GROSS proceeds that reaches the bank — the number to
        size deployment off. Reading it off the capital slice alone overstates
        it by ~5 points (0.73 vs 0.68 on the 2026 actuals)."""
        if not self.gross_proceeds_ils:
            return 0.0
        return self.net_proceeds_ils / self.gross_proceeds_ils


def sale_tax(
    shares: float,
    sale_price: float,
    benchmark: float,
    *,
    fx: float,
    prior_capital_income_ils: float = 0.0,
    salary_clears_threshold: bool = True,
) -> SaleTax:
    """The whole bill for selling ``shares`` of one grant: ``0.30(S-B) + 0.50B``.

    ``fx`` converts USD to ILS. ``prior_capital_income_ils`` is the capital
    income already realised this tax year, which consumes the 2% band's
    headroom — pass it when pacing a multi-lot or multi-year glide, or the
    second lot is charged at the first lot's rate.

    Sell LOWEST-benchmark first: a higher ``B`` moves money from the 30% slice
    into the 50% one, so total tax rises with the benchmark.
    """
    gross_ils = shares * sale_price * fx
    gain_ils = max(0.0, capital_gain_usd(shares, sale_price, benchmark) * fx)
    ordinary_ils = ordinary_income_usd(shares, benchmark) * fx
    return SaleTax(
        gross_proceeds_ils=round(gross_ils, 2),
        capital=capital_tax_ils(
            gain_ils,
            prior_capital_income_ils=prior_capital_income_ils,
            salary_clears_threshold=salary_clears_threshold,
        ),
        ordinary_income_ils=round(ordinary_ils, 2),
        ordinary_tax_ils=ordinary_tax_ils(ordinary_ils),
    )
