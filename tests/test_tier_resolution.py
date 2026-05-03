"""Tier resolution tests (SDD §4)."""

from __future__ import annotations

import pytest

from argosy.agent_settings import AgentSettings, TiersBlock
from argosy.decisions.tiers import (
    OverrideMode,
    Tier,
    TierContext,
    apply_override_mode,
    parse_override_mode,
    resolve_tier,
)


def _settings(override_mode: str = "auto") -> AgentSettings:
    return AgentSettings(tiers=TiersBlock(override_mode=override_mode))


def _ctx(**kwargs) -> TierContext:
    defaults = dict(
        proposed_value_usd=100.0,
        portfolio_value_usd=10_000.0,
        account_class="main",
        ticker="AAPL",
        is_nvda=False,
        is_plan_structural=False,
        crosses_concentration_cap=False,
        recent_red_flag=False,
        account_value_usd=10_000.0,
        in_known_watchlist=True,
        recent_material_news=False,
    )
    defaults.update(kwargs)
    return TierContext(**defaults)


def test_t0_auto_below_threshold_in_watchlist() -> None:
    """0.05% in watchlist, no news → T0."""
    ctx = _ctx(proposed_value_usd=5.0, portfolio_value_usd=10_000.0)
    assert resolve_tier(ctx, _settings()) == Tier.T0


def test_t0_blocked_off_watchlist() -> None:
    """Off-watchlist small trade does NOT get T0 even at 0.05%."""
    ctx = _ctx(
        proposed_value_usd=5.0,
        portfolio_value_usd=10_000.0,
        in_known_watchlist=False,
    )
    assert resolve_tier(ctx, _settings()) == Tier.T1


def test_t0_blocked_recent_news() -> None:
    """Recent material news on a small trade pushes off T0."""
    ctx = _ctx(
        proposed_value_usd=5.0,
        portfolio_value_usd=10_000.0,
        recent_material_news=True,
    )
    assert resolve_tier(ctx, _settings()) == Tier.T1


def test_t1_band() -> None:
    """0.5% portfolio → T1."""
    ctx = _ctx(proposed_value_usd=50.0, portfolio_value_usd=10_000.0)
    assert resolve_tier(ctx, _settings()) == Tier.T1


def test_t2_band() -> None:
    """3% portfolio → T2."""
    ctx = _ctx(proposed_value_usd=300.0, portfolio_value_usd=10_000.0)
    assert resolve_tier(ctx, _settings()) == Tier.T2


def test_t2_red_flag_promotes_small_trade() -> None:
    """< 1% small trade with red_flag → T2 (not T1)."""
    ctx = _ctx(
        proposed_value_usd=50.0, portfolio_value_usd=10_000.0, recent_red_flag=True
    )
    assert resolve_tier(ctx, _settings()) == Tier.T2


def test_t3_above_5pct() -> None:
    """7% → T3."""
    ctx = _ctx(proposed_value_usd=700.0, portfolio_value_usd=10_000.0)
    assert resolve_tier(ctx, _settings()) == Tier.T3


def test_nvda_always_t3() -> None:
    """Any NVDA trade auto-T3 regardless of size."""
    ctx = _ctx(
        proposed_value_usd=5.0, portfolio_value_usd=10_000.0, ticker="NVDA", is_nvda=True
    )
    assert resolve_tier(ctx, _settings()) == Tier.T3


def test_plan_structural_t3() -> None:
    ctx = _ctx(proposed_value_usd=50.0, is_plan_structural=True)
    assert resolve_tier(ctx, _settings()) == Tier.T3


def test_concentration_cap_cross_t3() -> None:
    ctx = _ctx(proposed_value_usd=50.0, crosses_concentration_cap=True)
    assert resolve_tier(ctx, _settings()) == Tier.T3


def test_account_scoped_escalation_limited_account() -> None:
    """Limited acct, > 20% of account → +1 tier."""
    # 0.5% of portfolio = T1; but 25% of small account → T2
    ctx = _ctx(
        proposed_value_usd=50.0,
        portfolio_value_usd=10_000.0,
        account_class="limited",
        account_value_usd=200.0,
    )
    assert resolve_tier(ctx, _settings()) == Tier.T2


def test_account_scoped_escalation_main_account_unaffected() -> None:
    ctx = _ctx(
        proposed_value_usd=50.0,
        portfolio_value_usd=10_000.0,
        account_class="main",
        account_value_usd=200.0,
    )
    assert resolve_tier(ctx, _settings()) == Tier.T1


def test_account_scoped_escalation_with_t3_clamps_at_t3() -> None:
    """Escalating from T3 keeps T3 (no T4)."""
    ctx = _ctx(
        proposed_value_usd=10_000.0,
        portfolio_value_usd=10_000.0,
        account_class="limited",
        account_value_usd=100.0,
    )
    assert resolve_tier(ctx, _settings()) == Tier.T3


def test_zero_portfolio_value_is_safe() -> None:
    ctx = _ctx(proposed_value_usd=100.0, portfolio_value_usd=0.0)
    # 0% computed → falls to T0 if watchlist, else T1
    assert resolve_tier(ctx, _settings()) == Tier.T0


# ----------------- Override modes -----------------


def test_override_auto_passthrough() -> None:
    ctx = _ctx(proposed_value_usd=50.0)
    auto = resolve_tier(ctx, _settings("auto"))
    assert apply_override_mode(auto, _settings("auto")) == Tier.T1


def test_override_pinned_floor() -> None:
    """pinned:T2 floors any decision below T2."""
    ctx = _ctx(proposed_value_usd=50.0)  # auto = T1
    auto = resolve_tier(ctx, _settings())
    assert auto == Tier.T1
    settings = _settings("pinned:T2")
    assert apply_override_mode(auto, settings) == Tier.T2


def test_override_pinned_does_not_demote() -> None:
    """If auto is T3, pinned:T2 stays at T3."""
    ctx = _ctx(proposed_value_usd=10_000.0, portfolio_value_usd=10_000.0)
    auto = resolve_tier(ctx, _settings())
    assert auto == Tier.T3
    settings = _settings("pinned:T2")
    assert apply_override_mode(auto, settings) == Tier.T3


def test_override_all_tier_forces_t3() -> None:
    ctx = _ctx(proposed_value_usd=5.0)
    auto = resolve_tier(ctx, _settings())  # T0
    assert apply_override_mode(auto, _settings("all-tier")) == Tier.T3


def test_override_per_decision_escalate() -> None:
    ctx = _ctx(proposed_value_usd=50.0)
    auto = resolve_tier(ctx, _settings())  # T1
    settings = _settings("per-decision-escalate")
    # Without bump, no change.
    assert apply_override_mode(auto, settings) == Tier.T1
    # With bump=1, T1 -> T2.
    assert (
        apply_override_mode(auto, settings, per_decision_bump_levels=1) == Tier.T2
    )


def test_parse_override_mode_unknown_falls_to_auto() -> None:
    mode, pinned = parse_override_mode("bogus")
    assert mode is OverrideMode.AUTO
    assert pinned is None


def test_tier_ordering_and_bump() -> None:
    assert Tier.T0 < Tier.T1 < Tier.T2 < Tier.T3
    assert Tier.T0.bump_up(2) == Tier.T2
    assert Tier.T2.bump_up(5) == Tier.T3  # clamps


def test_tier_from_str_invalid() -> None:
    with pytest.raises(ValueError):
        Tier.from_str("T5")
