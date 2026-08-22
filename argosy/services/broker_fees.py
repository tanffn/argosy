"""Deterministic per-trade commission arithmetic.

Why this exists (2026-08-22). Nothing in Argosy modelled transaction cost. A
grep for ``min_trade`` / ``minimum_ticket`` / ``min_order`` across ``argosy/``
returned zero hits, and ``deployment_advisor`` had no reference to commission at
all. So the engine sized buys with no idea that Leumi charges a **floor** per
trade — which is why the H1-2026 tape contains an ILS 11,376 buy that cost
0.18%, and why a proposed $3,000 slice would have paid ~0.7% in commission
alone.

Scope discipline (SDD "the LLM TEAM is the architecture"):
  * This module is **arithmetic only** — given a ticket size, what does the
    broker charge? That is the inviolable-arithmetic floor, the same class as
    conservation and estate/us-situs math.
  * It does **not** judge whether a trade is worth making, does not resize
    lines, and does not reject anything. "Is this ticket too small to be worth
    it?" is a judgment call and belongs to the team and to the user.
  * Callers annotate; they must not gate.

Rates live in ``domain_knowledge/brokers/leumi.md`` and are mirrored here as
constants. When that file is refreshed, update ``LEUMI_*`` below and the
``schedule_as_of`` date with it — the KB file is the source of truth for a human
reader, this module is the source of truth for the engine, and they must agree.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

__all__ = [
    "CommissionEstimate",
    "FeeSchedule",
    "LEUMI_FOREIGN",
    "LEUMI_ISRAELI",
    "estimate",
    "breakeven_ticket",
    "MINIMUM_DISPUTE_NOTE",
]


# --- The 20/08/2026 dispute -------------------------------------------------
# The Leumi benefits letter of 19/07/2026 prints 0.06% for 20/08/2026-31/12/2026
# with NO minimum line, where the preceding block explicitly carried "minimum
# $7". The letter's own boilerplate says benefits do not apply to minimum
# tariffs unless expressly stated, so the literal reading is that the LIST $25
# minimum returned. We model the conservative ($25) case and say so on every
# estimate. Do NOT flip this to 7.0 on a banker's verbal assurance — it takes an
# amended benefits letter. See domain_knowledge/brokers/leumi.md.
MINIMUM_DISPUTE_NOTE = (
    "Minimum is DISPUTED from 2026-08-20: the benefits letter dropped the "
    "explicit '$7 minimum' line when the rate moved to 0.06%, and its own "
    "boilerplate says benefits do not cover minimum tariffs unless stated. "
    "Modelling the conservative $25. Confirm in writing with Leumi."
)

LEUMI_MINIMUM_DISPUTE_FROM = date(2026, 8, 20)


@dataclass(frozen=True)
class FeeSchedule:
    """A broker's commission terms for one instrument class, one currency.

    ``rate`` is a fraction (0.0006 == 0.06%), ``minimum`` and ``maximum`` are in
    ``currency``. ``maximum_pct_of_ticket`` caps the fee on very small tickets
    (Leumi: 30% of the payment) and binds before ``minimum`` — without it a
    ILS 274 sale would be charged the full floor.
    """

    venue: str
    instrument_class: str
    currency: str
    rate: float
    minimum: float
    maximum: float | None = None
    maximum_pct_of_ticket: float | None = None
    disputed_minimum: float | None = None
    dispute_note: str = ""
    source: str = "domain_knowledge/brokers/leumi.md"


# Foreign securities via LeumiTrade, deposit 44745210. Minimum reflects the
# conservative reading of the 20/08/2026 change (see MINIMUM_DISPUTE_NOTE).
LEUMI_FOREIGN = FeeSchedule(
    venue="leumi",
    instrument_class="foreign_securities",
    currency="USD",
    rate=0.0006,
    minimum=25.0,
    maximum=7500.0,
    maximum_pct_of_ticket=0.30,
    disputed_minimum=7.0,
    dispute_note=MINIMUM_DISPUTE_NOTE,
)

# Israeli securities / non-linked bonds / TASE tracking funds via LeumiTrade.
# Undisputed: the ILS 7 minimum is printed explicitly and runs to 31/12/2026.
LEUMI_ISRAELI = FeeSchedule(
    venue="leumi",
    instrument_class="israeli_securities",
    currency="ILS",
    rate=0.0007,
    minimum=7.0,
    maximum=7000.0,
    maximum_pct_of_ticket=0.30,
)


@dataclass(frozen=True)
class CommissionEstimate:
    """What a single trade costs, and which clause decided it."""

    ticket: float
    commission: float
    currency: str
    #: "rate" | "minimum" | "maximum" | "maximum_pct_of_ticket"
    binding_rule: str
    #: commission / ticket, as a fraction. 0.0 for a zero ticket.
    cost_fraction: float
    #: True when the per-trade floor set the price — the ticket is below
    #: breakeven and the headline percentage is not what you are paying.
    below_breakeven: bool
    schedule: FeeSchedule
    note: str = ""

    @property
    def cost_bps(self) -> float:
        return round(self.cost_fraction * 10_000, 2)


def breakeven_ticket(schedule: FeeSchedule) -> float:
    """Ticket size at which the percentage first equals the floor.

    Below this, you pay the floor and the rate is decorative. Returns ``inf``
    when the schedule has no rate (a pure per-trade fee).
    """
    if schedule.rate <= 0:
        return float("inf")
    return schedule.minimum / schedule.rate


def estimate(ticket: float, schedule: FeeSchedule = LEUMI_FOREIGN) -> CommissionEstimate:
    """Commission on a single trade of ``ticket`` (in ``schedule.currency``).

    Order of application matches the tariff's own wording: the percentage is
    computed, raised to the minimum, then capped — first by the
    percentage-of-payment cap (which protects tiny tickets from the floor), then
    by the absolute maximum.

    A zero or negative ticket costs nothing; callers pass unsized lines through
    here rather than special-casing them.
    """
    if ticket <= 0:
        return CommissionEstimate(
            ticket=0.0, commission=0.0, currency=schedule.currency,
            binding_rule="rate", cost_fraction=0.0, below_breakeven=False,
            schedule=schedule,
        )

    fee = ticket * schedule.rate
    binding = "rate"
    if fee < schedule.minimum:
        fee = schedule.minimum
        binding = "minimum"

    # The 30%-of-payment cap binds BEFORE the absolute maximum and can undo the
    # minimum — on a ILS 274 sale the floor would otherwise exceed the cap.
    if schedule.maximum_pct_of_ticket is not None:
        cap = ticket * schedule.maximum_pct_of_ticket
        if fee > cap:
            fee, binding = cap, "maximum_pct_of_ticket"
    if schedule.maximum is not None and fee > schedule.maximum:
        fee, binding = schedule.maximum, "maximum"

    fee = round(fee, 2)
    note = schedule.dispute_note if schedule.disputed_minimum is not None else ""
    return CommissionEstimate(
        ticket=round(ticket, 2),
        commission=fee,
        currency=schedule.currency,
        binding_rule=binding,
        cost_fraction=fee / ticket,
        below_breakeven=binding in ("minimum", "maximum_pct_of_ticket"),
        schedule=schedule,
        note=note,
    )


def describe(est: CommissionEstimate) -> str:
    """One-line human summary for a proposal line or caveat."""
    sym = "$" if est.currency == "USD" else "ILS "
    base = (
        f"commission {sym}{est.commission:,.2f} on {sym}{est.ticket:,.2f} "
        f"({est.cost_bps:.1f} bps)"
    )
    if est.below_breakeven:
        be = breakeven_ticket(est.schedule)
        base += (
            f" — the per-trade floor set this price; breakeven ticket is "
            f"{sym}{be:,.0f}"
        )
    return base
