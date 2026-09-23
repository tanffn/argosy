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


# The former $25-vs-$7 dispute read only page 1. Pages 7–9 of the SAME
# 19/07/2026 letter explicitly grant 0.06%/$6 (direct upper tier 0.05%/$6)
# through 30/06/2027. Its greatest-benefit clause resolves the overlap.
# Independent source-only derivation: knowledge_repair/leumi_source_derivation.
# Keep the exported name for callers; it no longer asserts an unresolved minimum.
MINIMUM_DISPUTE_NOTE = (
    "Leumi letter 2026-07-19, pp.7–9: foreign-securities planning estimate "
    "0.06% / $6 minimum, valid 2026-07-01 through 2027-06-30, absent later "
    "amendments. Direct-channel upper portfolio tier may qualify for 0.05%; "
    "this estimate does not assume current tier eligibility. Separately "
    "chargeable exchange, broker and transfer expenses are excluded."
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


# Foreign securities: lower-tier/direct or banker schedule. No current book
# value is inferred; this is not the best-price selector for the upper tier.
LEUMI_FOREIGN = FeeSchedule(
    venue="leumi",
    instrument_class="foreign_securities",
    currency="USD",
    rate=0.0006,
    minimum=6.0,
    maximum=7500.0,
    maximum_pct_of_ticket=0.30,
    dispute_note=MINIMUM_DISPUTE_NOTE,
)

# Israeli securities / non-linked bonds / TASE tracking funds via LeumiTrade.
# Original row, explicitly valid to 31/12/2026; NOT a best-price selector.
# A competing 0.06%/ILS9 (upper direct 0.05%/ILS9) row can be cheaper for
# larger tickets. Callers selecting a schedule must compare complete rows.
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
    note = schedule.dispute_note
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
