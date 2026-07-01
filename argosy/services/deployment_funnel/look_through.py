from __future__ import annotations

# Explicit, versioned fund->constituent weight map for the HOUSEHOLD's held
# broad funds + candidate ETFs. NOT a live holdings feed — a small, cited,
# hand-maintained table sufficient for the correlated-exposure cap. Weights are
# fractions of the fund's NAV. Sources: index fact sheets (S&P 500, Russell 1000
# Growth) as of 2026-Q2; update LOOKTHROUGH_VERSION when refreshed.
LOOKTHROUGH_VERSION = 1

LOOKTHROUGH_MAP: dict[str, dict[str, float]] = {
    # US broad / growth — carry index NVDA weight.
    "CSPX": {"nvda": 0.07, "us": 1.00},   # iShares Core S&P 500 UCITS
    "VOO": {"nvda": 0.07, "us": 1.00},
    "FUSA": {"nvda": 0.06, "us": 1.00},   # Fidelity US Quality Income
    "R1GR": {"nvda": 0.14, "us": 1.00},   # iShares Russell 1000 Growth
    "SCHG": {"nvda": 0.13, "us": 1.00},
    "QQQM": {"nvda": 0.08, "us": 1.00},
    "SPMV": {"nvda": 0.01, "us": 1.00},   # min-vol underweights NVDA
    "SPMO": {"nvda": 0.10, "us": 1.00},
    # World funds — partial US, small NVDA.
    "FWRA": {"nvda": 0.04, "us": 0.65},
    "ACWD": {"nvda": 0.04, "us": 0.63},
    "IWDA": {"nvda": 0.05, "us": 0.70},
    "EXUS": {"nvda": 0.00, "us": 0.00},   # World ex-US
    "EIMI": {"nvda": 0.00, "us": 0.00},   # EM
    # Alternatives / cash-like — zero NVDA, zero US-equity.
    "SGLD": {"nvda": 0.00, "us": 0.00},   # gold ETC
    "IGLN": {"nvda": 0.00, "us": 0.00},
    "SGOV": {"nvda": 0.00, "us": 0.00},
    "IB01": {"nvda": 0.00, "us": 0.00},
    "IBTA": {"nvda": 0.00, "us": 0.00},
    # Direct single-name.
    "NVDA": {"nvda": 1.00, "us": 1.00},
}


def _weight(symbol: str, key: str) -> float:
    return LOOKTHROUGH_MAP.get(symbol.upper(), {}).get(key, 0.0)


def effective_nvda_usd(symbol: str, notional_usd: float) -> float:
    """Dollars of NVDA exposure a buy of ``notional_usd`` in ``symbol`` adds,
    including index look-through. Unknown symbols contribute 0 (caller tracks
    misses)."""
    return round(notional_usd * _weight(symbol, "nvda"), 2)


def effective_us_usd(symbol: str, notional_usd: float) -> float:
    return round(notional_usd * _weight(symbol, "us"), 2)
