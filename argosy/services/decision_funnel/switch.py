"""Sell-to-fund SWITCH simulation (step 8 — the funding-aware engine).

When the funding gate classifies an approved BUY as a ``switch_candidate`` (cash
short, but an eligible holding could be trimmed to fund it), this module
simulates the paired SELL → BUY as ONE decision: which tax lots to sell, the
gross proceeds, the estimated capital-gains tax and friction, and the
net-fundable amount.

HONESTY (codex review — output-trust doctrine):
  * Tax-lot selection is HIFO (highest cost basis first) to minimise the
    realised gain on a long-hold book.
  * The CGT estimate uses the Israeli capital-gains rate (``FALLBACK_IL_CGT_RATE``,
    a documented estimate — real IL tax is a capital/ordinary split and is
    computed on the NIS cost basis with FX, which we do NOT have here). So the
    estimate is marked DEGRADED on the FX/NIS-basis axis.
  * Friction (commission + spread + FX cost) has NO sourced constant in the
    system, so it is NOT fabricated: when no friction estimate is supplied the
    simulation is marked DEGRADED and ``net_fundable_usd`` is left ``None`` —
    the switch must stay shadow-only and must NOT be surfaced as a confident
    recommendation until friction + FX basis are real.
  * The "materially exceeds" decision is a NAMED, config-owned threshold (not a
    model phrase) — high enough that routine rebalancing can't become a switch.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date

from argosy.services.cash_source_reconciler import FALLBACK_IL_CGT_RATE

# Long-term holding-period cutoff. Israeli CGT does not have the US 1-year
# short/long split, but holding period is still recorded (some instruments /
# future rules care), so we tag it rather than act on it.
_LT_DAYS = 365


@dataclass(frozen=True)
class SwitchPolicy:
    """Config-owned thresholds for when a sell-to-fund switch is allowed.

    Deliberately strict so routine rebalancing can never masquerade as a
    'fantastic deal' switch (codex Q4)."""

    # The buy's conviction score must exceed the funding source's by at least
    # this much (on a 0-3 bucket scale: LOW=1, MED=2, HIGH=3). 2 = a two-bucket
    # gap, e.g. HIGH buy vs LOW source.
    min_conviction_delta: float = 2.0
    # The funding source's conviction must be no higher than this (don't sell a
    # high-conviction holding to fund another).
    max_source_conviction: float = 1.0  # LOW
    # The net benefit after tax+friction must be positive by at least this much
    # to bother (a switch that barely breaks even isn't worth the tax event).
    min_net_benefit_usd: float = 0.0


DEFAULT_SWITCH_POLICY = SwitchPolicy()

_CONVICTION_SCORE = {"HIGH": 3.0, "MED": 2.0, "MEDIUM": 2.0, "LOW": 1.0}


def conviction_score(label: str | None) -> float:
    return _CONVICTION_SCORE.get((label or "").upper(), 0.0)


@dataclass(frozen=True)
class LotInput:
    """One tax lot of the funding source (mirrors the relevant Lot columns)."""

    lot_id: str
    quantity: float
    cost_basis_usd: float  # total basis for the lot (not per-share)
    acquired_at: date | None = None

    @property
    def cost_per_share(self) -> float:
        return self.cost_basis_usd / self.quantity if self.quantity else 0.0


@dataclass(frozen=True)
class SellLeg:
    ticker: str
    quantity: float
    gross_proceeds_usd: float
    cost_basis_usd: float
    realized_gain_usd: float
    estimated_cgt_usd: float
    cgt_rate: float
    lot_ids: list[str]
    holding_period: str  # "long" | "short" | "mixed" | "unknown"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SwitchSimulation:
    buy_ticker: str
    shortfall_usd: float  # cash still needed for the buy after available cash
    sell: SellLeg | None
    gross_proceeds_usd: float | None
    estimated_cgt_usd: float | None
    estimated_friction_usd: float | None
    net_fundable_usd: float | None
    covers_shortfall: bool
    degraded: bool
    degraded_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    lot_selection_summary: str = ""

    def to_dict(self) -> dict:
        return {
            "buy_ticker": self.buy_ticker,
            "shortfall_usd": self.shortfall_usd,
            "sell": self.sell.to_dict() if self.sell else None,
            "gross_proceeds_usd": self.gross_proceeds_usd,
            "estimated_cgt_usd": self.estimated_cgt_usd,
            "estimated_friction_usd": self.estimated_friction_usd,
            "net_fundable_usd": self.net_fundable_usd,
            "covers_shortfall": self.covers_shortfall,
            "degraded": self.degraded,
            "degraded_reasons": self.degraded_reasons,
            "warnings": self.warnings,
            "lot_selection_summary": self.lot_selection_summary,
        }


def _holding_period(acquired: date | None, as_of: date) -> str:
    if acquired is None:
        return "unknown"
    return "long" if (as_of - acquired).days >= _LT_DAYS else "short"


def should_switch(
    *,
    buy_conviction: str | None,
    source_conviction: str | None,
    policy: SwitchPolicy = DEFAULT_SWITCH_POLICY,
) -> tuple[bool, str]:
    """Whether the conviction gap clears the (strict) switch bar. Returns
    ``(allowed, reason)``. Net-benefit is checked separately once the sim runs."""
    buy_s = conviction_score(buy_conviction)
    src_s = conviction_score(source_conviction)
    if buy_s < _CONVICTION_SCORE["HIGH"]:
        return False, "switch requires a HIGH-conviction buy"
    # Unknown / ungraded source conviction is NOT evidence of low conviction —
    # we must not sell a holding to fund another on an assumption. Require a
    # KNOWN, low-enough source conviction (codex BLOCKER).
    if src_s <= 0.0:
        return False, (
            "funding source conviction unknown — cannot confirm it's low enough to sell"
        )
    if src_s > policy.max_source_conviction:
        return False, (
            f"funding source conviction too high ({source_conviction}) — don't "
            "sell a conviction holding to fund another"
        )
    if (buy_s - src_s) < policy.min_conviction_delta:
        return False, (
            f"conviction gap {buy_s - src_s:.0f} below the {policy.min_conviction_delta:.0f} "
            "bar — routine swap, not a switch"
        )
    return True, (
        f"buy conviction materially exceeds source (gap {buy_s - src_s:.0f})"
    )


def simulate_switch(
    *,
    buy_ticker: str,
    shortfall_usd: float,
    source_ticker: str,
    lots: list[LotInput],
    current_price_usd: float | None,
    as_of: date,
    cgt_rate: float = FALLBACK_IL_CGT_RATE,
    friction_usd: float | None = None,
) -> SwitchSimulation:
    """Simulate selling enough of ``source_ticker`` (HIFO lots) to fund a
    ``shortfall_usd`` buy of ``buy_ticker``. Deterministic; honest about what it
    can't compute (no price / no friction / FX basis)."""
    # The CGT figure can NEVER be authoritative here: real Israeli tax is computed
    # on the NIS cost basis with FX, which this model does not have. So a switch
    # is ALWAYS degraded on that axis — it must never read as a confident,
    # non-degraded recommendation (codex BLOCKER).
    degraded_reasons: list[str] = [
        "CGT estimate is USD-based; Israeli tax uses the NIS cost basis + FX — estimate only",
    ]
    warnings: list[str] = list(degraded_reasons)

    if current_price_usd is None or current_price_usd <= 0 or not lots:
        return SwitchSimulation(
            buy_ticker=buy_ticker, shortfall_usd=shortfall_usd, sell=None,
            gross_proceeds_usd=None, estimated_cgt_usd=None,
            estimated_friction_usd=friction_usd, net_fundable_usd=None,
            covers_shortfall=False, degraded=True,
            degraded_reasons=["no current price or no tax lots for the source"],
            warnings=warnings,
            lot_selection_summary="cannot simulate — missing price or lots",
        )

    # HIFO: highest cost-per-share first → smallest realised gain.
    ordered = sorted(lots, key=lambda l: l.cost_per_share, reverse=True)

    # Select lots until GROSS proceeds cover the shortfall. We size on gross
    # (the minimum to raise the cash); the after-tax/friction drag is reported
    # so the caller sees whether it actually clears the shortfall net.
    sel_qty = 0.0
    sel_basis = 0.0
    sel_ids: list[str] = []
    periods: set[str] = set()
    for lot in ordered:
        if sel_qty * current_price_usd >= shortfall_usd:
            break
        # How many shares of THIS lot do we still need?
        remaining_usd = shortfall_usd - sel_qty * current_price_usd
        shares_needed = remaining_usd / current_price_usd
        take = min(lot.quantity, shares_needed)
        frac = take / lot.quantity if lot.quantity else 0.0
        sel_qty += take
        sel_basis += lot.cost_basis_usd * frac
        sel_ids.append(lot.lot_id)
        periods.add(_holding_period(lot.acquired_at, as_of))

    gross = round(sel_qty * current_price_usd, 2)
    realized_gain = round(gross - sel_basis, 2)
    cgt = round(max(realized_gain, 0.0) * cgt_rate, 2)
    if "unknown" in periods:
        degraded_reasons.append("a selected lot has no acquisition date — holding period unknown")
    holding = periods.pop() if len(periods) == 1 else ("mixed" if periods else "unknown")

    net_fundable: float | None
    if friction_usd is None:
        degraded_reasons.append(
            "no friction estimate (commission/spread/FX) — not fabricated; switch stays shadow-only"
        )
        net_fundable = None
        covers = False
    else:
        net_fundable = round(gross - cgt - friction_usd, 2)
        covers = net_fundable >= shortfall_usd

    sell = SellLeg(
        ticker=source_ticker, quantity=round(sel_qty, 6), gross_proceeds_usd=gross,
        cost_basis_usd=round(sel_basis, 2), realized_gain_usd=realized_gain,
        estimated_cgt_usd=cgt, cgt_rate=cgt_rate, lot_ids=sel_ids,
        holding_period=holding,
    )
    summary = (
        f"sell {sel_qty:.4g} {source_ticker} from {len(sel_ids)} lot(s) (HIFO) "
        f"→ ~${gross:,.0f} gross, ~${cgt:,.0f} est. CGT"
    )
    # A switch on a degraded estimate must never read as confidently fundable.
    if friction_usd is None and gross < shortfall_usd:
        warnings.append("gross proceeds alone do not cover the shortfall before tax")

    return SwitchSimulation(
        buy_ticker=buy_ticker, shortfall_usd=shortfall_usd, sell=sell,
        gross_proceeds_usd=gross, estimated_cgt_usd=cgt,
        estimated_friction_usd=friction_usd, net_fundable_usd=net_fundable,
        covers_shortfall=covers, degraded=bool(degraded_reasons),
        degraded_reasons=degraded_reasons, warnings=warnings,
        lot_selection_summary=summary,
    )


__all__ = [
    "DEFAULT_SWITCH_POLICY",
    "LotInput",
    "SellLeg",
    "SwitchPolicy",
    "SwitchSimulation",
    "conviction_score",
    "should_switch",
    "simulate_switch",
]
