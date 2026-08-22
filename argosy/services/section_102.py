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
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

__all__ = [
    "SURTAX_THRESHOLD_ILS",
    "STATUTORY_CGT",
    "GENERAL_SURTAX",
    "CAPITAL_SOURCE_SURTAX",
    "TaxBands",
    "grant_benchmark",
    "capital_gain_usd",
    "capital_tax_ils",
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

    The ordinary slice (``shares x benchmark``) is settled at VEST via
    sell-to-cover and is NOT recomputed here.
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
