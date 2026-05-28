"""Behavioral guardrails — pre-trade checkpoints (MED #28).

Pre-trade interception when proposed action matches a known bias pattern.
Fires a cooldown / confirmation modal rather than blocking outright.
"""
from dataclasses import dataclass
from typing import Literal


CheckpointKind = Literal["panic_sell", "fomo_buy", "anchoring", "recency_bias"]


@dataclass(frozen=True)
class BehavioralCheckpoint:
    kind: CheckpointKind
    triggered: bool
    rationale: str
    cooldown_hours: int  # e.g. 24h pause before order routes
    confirmation_prompt: str


def check_panic_sell(
    *,
    proposed_sell_pct: float,
    days_since_market_peak: int,
    peak_to_now_drawdown_pct: float,
) -> BehavioralCheckpoint:
    """Fires when user proposes a meaningful sell after a recent drawdown."""
    triggered = (
        proposed_sell_pct >= 0.10  # selling 10%+ of holdings
        and peak_to_now_drawdown_pct >= 0.15  # market down 15%+
        and days_since_market_peak <= 90  # within 3 months of peak
    )
    return BehavioralCheckpoint(
        kind="panic_sell",
        triggered=triggered,
        rationale=(
            f"Proposing {proposed_sell_pct:.0%} sell after a "
            f"{peak_to_now_drawdown_pct:.0%} drawdown within "
            f"{days_since_market_peak} days of peak. Classic panic-sell "
            "pattern; historical evidence strongly favors holding."
        ) if triggered else "No panic-sell pattern detected.",
        cooldown_hours=24 if triggered else 0,
        confirmation_prompt=(
            "You're proposing to sell after a recent drawdown. Selling "
            "during stress historically locks in losses. Wait 24h?"
        ) if triggered else "",
    )


def check_fomo_buy(
    *,
    proposed_buy_pct: float,
    asset_30d_return_pct: float,
    asset_concentration_pct: float,
) -> BehavioralCheckpoint:
    """Fires when user proposes a large buy of a recently-skyrocketed asset."""
    triggered = (
        proposed_buy_pct >= 0.05  # adding 5%+ to a single position
        and asset_30d_return_pct >= 0.30  # asset up 30%+ in 30d
        and asset_concentration_pct >= 0.20  # already 20%+ of portfolio
    )
    return BehavioralCheckpoint(
        kind="fomo_buy",
        triggered=triggered,
        rationale=(
            f"Proposing {proposed_buy_pct:.0%} buy of an asset already "
            f"{asset_concentration_pct:.0%} of portfolio that's up "
            f"{asset_30d_return_pct:.0%} in 30 days. FOMO pattern; "
            "mean-reversion is the historical baseline at these levels."
        ) if triggered else "No FOMO pattern detected.",
        cooldown_hours=24 if triggered else 0,
        confirmation_prompt=(
            "You're adding to a concentrated position that's recently "
            "surged. Recent strong returns predict mean-reversion. Wait 24h?"
        ) if triggered else "",
    )
