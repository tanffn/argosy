from __future__ import annotations

# Holdings keys that already satisfy the reserve (cash + T-bill/short-Treasury
# ETFs). A new T-bill/cash candidate must not be recommended on top of these.
CASH_LIKE_SYMBOLS = frozenset(
    {"SGOV", "IB01", "IBTA", "ERNS", "CASH_USD", "CASH_NIS"}
)


def existing_cash_like_usd(holdings_usd: dict[str, float]) -> float:
    return round(
        sum(v for s, v in holdings_usd.items() if s.upper() in CASH_LIKE_SYMBOLS),
        2,
    )


def reserve_shortfall_usd(
    book_usd: float, holdings_usd: dict[str, float], reserve_target_pct: float
) -> float:
    """How much MORE cash-like the book needs to hit the reserve target. 0 when
    already funded — the signal that a T-bill/cash candidate is redundant."""
    target = book_usd * reserve_target_pct / 100.0
    have = existing_cash_like_usd(holdings_usd)
    return round(max(0.0, target - have), 2)
