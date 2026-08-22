"""Household operating-cash floor — the shekels that are never deployable.

Ariel's standing constraint (2026-08-22): **at least ILS 30,000 must remain in
the current account for ongoing expenses.** That money funds the mortgage
standing order, the credit-card charges and daily spending; it is not
investable cash, and any deploy plan that spends it is wrong regardless of how
attractive the allocation is.

Distinct from ``discovery_reserve``, which earmarks dry powder for *future
opportunistic buys* and is a plan/strategy choice. This one is a household
liquidity requirement and is deducted FIRST — before the discovery reserve and
before any allocation math sees the money.

Denominated in ILS on purpose: the obligations it covers (mortgage, cards,
municipal bills) are shekel obligations, so a fixed USD figure would drift with
FX. Callers supply the rate; there is no silent default, because a wrong rate
here silently under-reserves the household's rent money.
"""

from __future__ import annotations

__all__ = [
    "OPERATING_FLOOR_ILS",
    "operating_floor_usd",
    "apply_operating_floor",
    "labeled_operating_floor",
    "OPERATING_FLOOR_LABEL",
]

#: Standing household liquidity floor, in shekels. Set by Ariel 2026-08-22.
#: Changing this is a user decision, never an engine inference.
OPERATING_FLOOR_ILS: float = 30_000.0

OPERATING_FLOOR_LABEL = "Operating cash floor (household expenses)"


def operating_floor_usd(usd_ils: float | None) -> float:
    """The floor converted to USD at ``usd_ils`` (shekels per dollar).

    Returns 0.0 when the rate is missing or non-positive. That is deliberately
    the SAFE direction for the caller to notice — a zero floor shows up as
    "no reserve was withheld" in the plan, which is visible, whereas guessing a
    rate would silently withhold the wrong amount of the household's rent.
    Callers that cannot supply a rate should say so in a caveat.
    """
    if not usd_ils or float(usd_ils) <= 0:
        return 0.0
    return round(OPERATING_FLOOR_ILS / float(usd_ils), 2)


def apply_operating_floor(
    *, cash_total_usd: float, floor_usd: float
) -> tuple[float, float]:
    """Split cash into ``(deployable, withheld_floor)``.

    Mirrors ``discovery_reserve.apply_discovery_reserve`` exactly, including its
    edge cases: a floor larger than the cash withholds everything rather than
    going negative, and negative inputs clamp to zero.
    """
    cash = max(0.0, round(float(cash_total_usd), 2))
    floor = max(0.0, round(float(floor_usd), 2))
    if floor <= 0 or cash <= 0:
        return cash, 0.0
    withheld = min(floor, cash)
    return round(cash - withheld, 2), round(withheld, 2)


def labeled_operating_floor(withheld_usd: float, usd_ils: float | None) -> str:
    """One caveat line naming the floor, in both currencies."""
    if usd_ils and float(usd_ils) > 0:
        return (
            f"{OPERATING_FLOOR_LABEL}: ${withheld_usd:,.2f} "
            f"(ILS {OPERATING_FLOOR_ILS:,.0f} at {float(usd_ils):.4f}) held back "
            f"for ongoing household expenses and excluded from deployable cash."
        )
    return (
        f"{OPERATING_FLOOR_LABEL}: ILS {OPERATING_FLOOR_ILS:,.0f} could NOT be "
        f"withheld — no USD/ILS rate was available, so the full cash balance is "
        f"shown as deployable. Confirm the household buffer before ordering."
    )
