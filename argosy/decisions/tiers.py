"""Tier resolution (SDD §4, Phase 3).

Maps a proposed transaction (size, ticker, account, plan-impact) to a
Tier (T0/T1/T2/T3) per SDD §4.1. Reads thresholds from `agent_settings.tiers`.

Special rules per SDD §4.3:
- NVDA-specific override: any NVDA buy/sell of any size is automatically T3
- Plan-structural changes: any move that crosses concentration cap or
  changes plan structure → T3
- Account-scoped escalation: limited account, single trade > 20% of
  account value → escalate up one tier (caps damage if the agent goes
  off the rails on the small account)
- Tier descent disallowed once decision opened (caller's responsibility)

Override modes per SDD §4.4:
- `auto` — use the resolved tier
- `pinned:T<n>` — bump up to T<n> if resolved tier is below
- `all-tier` — every decision runs full T3 stack
- `per-decision-escalate` — UI bumps a single proposal up one tier;
  caller passes the bumped-tier in directly
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Literal

from argosy.agent_settings import AgentSettings


class Tier(str, enum.Enum):
    """Decision tier per SDD §4.1.

    Ordered. `Tier.T0 < Tier.T3`. Use `Tier.from_str` for case-insensitive
    parsing of CLI / config inputs.
    """

    T0 = "T0"
    T1 = "T1"
    T2 = "T2"
    T3 = "T3"

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Tier):
            return NotImplemented
        return _TIER_ORDER[self] < _TIER_ORDER[other]

    def __le__(self, other: object) -> bool:
        if not isinstance(other, Tier):
            return NotImplemented
        return _TIER_ORDER[self] <= _TIER_ORDER[other]

    def bump_up(self, levels: int = 1) -> "Tier":
        idx = min(3, _TIER_ORDER[self] + max(0, levels))
        return _TIER_BY_INDEX[idx]

    @classmethod
    def from_str(cls, raw: str) -> "Tier":
        s = (raw or "").strip().upper()
        if s not in cls.__members__:
            raise ValueError(f"unknown tier: {raw!r}")
        return cls.__members__[s]


_TIER_ORDER: dict[Tier, int] = {Tier.T0: 0, Tier.T1: 1, Tier.T2: 2, Tier.T3: 3}
_TIER_BY_INDEX: dict[int, Tier] = {0: Tier.T0, 1: Tier.T1, 2: Tier.T2, 3: Tier.T3}


@dataclass
class TierContext:
    """Inputs to `resolve_tier`. All fields are required so callers stay honest.

    Attributes:
      proposed_value_usd: dollar size of the proposed trade.
      portfolio_value_usd: total portfolio value (denominator for the
        portfolio-pct rules).
      account_class: 'main' or 'limited' (Argonaut). Limited triggers
        the 20% account-scoped escalation rule.
      ticker: the proposed ticker (NVDA gets the special override).
      is_nvda: True iff the ticker is NVDA (caller resolves; allows tests
        and future alias handling). Convenience; if false but ticker is
        'NVDA', the resolver still applies the override.
      is_plan_structural: True iff the proposal changes plan structure
        (allocation target, schedule, cap). Auto-T3.
      crosses_concentration_cap: True iff the proposal would push any
        category over a configured cap. Auto-T3.
      recent_red_flag: True iff the ticker has a flagged event (RED
        plan-critique finding, news-analyst high-materiality story, etc.)
        within the recency window. Bumps a small trade from T1 to T2.
      account_value_usd: value of the specific account the proposal
        would execute in. Used by the 20% account-scoped rule.
      in_known_watchlist: True iff the ticker is in the user's
        watchlist. T0 requires this.
      recent_material_news: True iff the ticker has any recent material
        news (regardless of RED). T0 requires this to be False.
    """

    proposed_value_usd: float
    portfolio_value_usd: float
    account_class: Literal["main", "limited"]
    ticker: str
    is_nvda: bool
    is_plan_structural: bool
    crosses_concentration_cap: bool
    recent_red_flag: bool
    account_value_usd: float
    in_known_watchlist: bool = True
    recent_material_news: bool = False

    def portfolio_pct(self) -> float:
        if self.portfolio_value_usd <= 0:
            return 0.0
        return (self.proposed_value_usd / self.portfolio_value_usd) * 100.0

    def account_pct(self) -> float:
        if self.account_value_usd <= 0:
            return 0.0
        return (self.proposed_value_usd / self.account_value_usd) * 100.0


def resolve_tier(ctx: TierContext, settings: AgentSettings) -> Tier:
    """Compute the AUTO tier per SDD §4.1.

    The override mode is applied separately by `apply_override_mode` so
    the caller has visibility into both the auto value and the post-
    override value (audit log records both).
    """
    tiers_cfg = settings.tiers

    # T3 hard rules first (NVDA, plan structure, concentration cross).
    is_nvda = ctx.is_nvda or ctx.ticker.upper() == "NVDA"
    if is_nvda or ctx.is_plan_structural or ctx.crosses_concentration_cap:
        return _apply_account_escalation(Tier.T3, ctx, tiers_cfg.account_scoped_escalation_pct)

    pct = ctx.portfolio_pct()

    # T3 if > 5%
    if pct > tiers_cfg.t2_max_portfolio_pct:
        base = Tier.T3
    # T2 if 1-5%
    elif pct > tiers_cfg.t1_max_portfolio_pct:
        base = Tier.T2
    # T2 if < 1% but on a flagged ticker (recent_red_flag)
    elif ctx.recent_red_flag:
        base = Tier.T2
    # T1 if 0.1-1%
    elif pct > tiers_cfg.t0_max_portfolio_pct:
        base = Tier.T1
    # T0 only if in watchlist AND no recent material news
    elif ctx.in_known_watchlist and not ctx.recent_material_news:
        base = Tier.T0
    else:
        # Off-watchlist or fresh-news small trade gets standard tier (T1).
        base = Tier.T1

    return _apply_account_escalation(base, ctx, tiers_cfg.account_scoped_escalation_pct)


def _apply_account_escalation(
    base: Tier, ctx: TierContext, escalation_pct: float
) -> Tier:
    """Limited account, single trade > 20% of account value → +1 tier."""
    if ctx.account_class != "limited":
        return base
    if ctx.account_value_usd <= 0:
        return base
    if ctx.account_pct() > escalation_pct:
        return base.bump_up(1)
    return base


# ----------------------------------------------------------------------
# Override modes
# ----------------------------------------------------------------------


class OverrideMode(str, enum.Enum):
    AUTO = "auto"
    ALL_TIER = "all-tier"
    PER_DECISION_ESCALATE = "per-decision-escalate"
    # `pinned:T<n>` strings are parsed via parse_override_mode below.


def parse_override_mode(raw: str) -> tuple[OverrideMode, Tier | None]:
    """Parse an override-mode string from agent_settings.

    Returns (mode, pinned_tier). For `pinned:T2`, pinned_tier=Tier.T2;
    else None. Defaults to AUTO on unknown strings.
    """
    s = (raw or "").strip().lower()
    if s in ("", "auto"):
        return OverrideMode.AUTO, None
    if s == "all-tier":
        return OverrideMode.ALL_TIER, None
    if s == "per-decision-escalate":
        return OverrideMode.PER_DECISION_ESCALATE, None
    if s.startswith("pinned:"):
        try:
            tier = Tier.from_str(s.split(":", 1)[1])
            return OverrideMode.AUTO, tier  # AUTO marker; pinned_tier carries floor
        except ValueError:
            return OverrideMode.AUTO, None
    return OverrideMode.AUTO, None


def apply_override_mode(
    auto: Tier,
    settings: AgentSettings,
    *,
    per_decision_bump_levels: int = 0,
) -> Tier:
    """Apply the configured override mode to the auto tier.

    Args:
      auto: the tier resolved by `resolve_tier`.
      settings: parsed agent_settings.
      per_decision_bump_levels: when the user clicks "Escalate-tier" on a
        proposal in the UI, this is set to the number of tiers to bump
        (typically 1). Only meaningful when override_mode is
        `per-decision-escalate`. Other modes ignore it.

    Returns:
      The effective tier after override application.
    """
    raw = settings.tiers.override_mode
    mode, pinned = parse_override_mode(raw)

    if mode is OverrideMode.ALL_TIER:
        return Tier.T3
    if mode is OverrideMode.PER_DECISION_ESCALATE and per_decision_bump_levels > 0:
        return auto.bump_up(per_decision_bump_levels)
    if pinned is not None:
        # `pinned:T<n>` floor: take the max of auto and pinned.
        return pinned if auto < pinned else auto
    return auto


__all__ = [
    "OverrideMode",
    "Tier",
    "TierContext",
    "apply_override_mode",
    "parse_override_mode",
    "resolve_tier",
]
