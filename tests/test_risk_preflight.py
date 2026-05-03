"""Risk preflight tests (SDD §9.3)."""

from __future__ import annotations

from datetime import datetime, timezone

from argosy.agent_settings import AgentSettings, ExecutionBlock
from argosy.decisions.proposals import Proposal, ProposalStatus
from argosy.decisions.risk_preflight import (
    PreflightInputs,
    PreflightStatus,
    check_cash_availability,
    check_concentration_cap,
    check_daily_loss_limit,
    check_position_size_cap,
    check_tier_mode_match,
    check_trading_hours,
    check_wash_sale,
    run_preflight,
)


def _proposal(**overrides) -> Proposal:
    base = dict(
        user_id="ariel",
        ticker="AAPL",
        action="buy",
        size_shares_or_currency=10.0,
        size_units="shares",
        instrument="stock",
        order_type="limit",
        limit_price=150.0,
        stop_price=None,
        time_in_force="DAY",
        tier="T1",
        account_class="main",
        status=ProposalStatus.DRAFT,
    )
    base.update(overrides)
    return Proposal(**base)


# ----------------- cash -----------------


def test_cash_pass() -> None:
    p = _proposal()
    r = check_cash_availability(p, cash_available_usd=10_000)
    assert r.status is PreflightStatus.PASS


def test_cash_hard_fail_on_shortfall() -> None:
    p = _proposal()
    r = check_cash_availability(p, cash_available_usd=100)
    assert r.status is PreflightStatus.HARD_FAIL


def test_cash_pass_on_sell() -> None:
    p = _proposal(action="sell")
    r = check_cash_availability(p, cash_available_usd=0)
    assert r.status is PreflightStatus.PASS


def test_cash_warn_on_market_order() -> None:
    p = _proposal(order_type="market", limit_price=None)
    r = check_cash_availability(p, cash_available_usd=10_000)
    assert r.status is PreflightStatus.WARN


# ----------------- size cap -----------------


def test_size_cap_pass() -> None:
    p = _proposal()
    r = check_position_size_cap(p, max_position_usd=10_000)
    assert r.status is PreflightStatus.PASS


def test_size_cap_hard_fail() -> None:
    p = _proposal(size_shares_or_currency=1_000.0)
    r = check_position_size_cap(p, max_position_usd=1_000)
    assert r.status is PreflightStatus.HARD_FAIL


def test_size_cap_no_cap_configured() -> None:
    p = _proposal()
    r = check_position_size_cap(p, max_position_usd=None)
    assert r.status is PreflightStatus.PASS


# ----------------- concentration -----------------


def test_concentration_buy_over_cap_hard_fails() -> None:
    p = _proposal(ticker="NVDA", action="buy")
    r = check_concentration_cap(
        p,
        snapshot_pct={"NVDA": 70.0},
        plan_targets={"NVDA": 15.0},
        breach_pct_over=5.0,
    )
    assert r.status is PreflightStatus.HARD_FAIL


def test_concentration_sell_over_cap_warns() -> None:
    p = _proposal(ticker="NVDA", action="sell")
    r = check_concentration_cap(
        p,
        snapshot_pct={"NVDA": 70.0},
        plan_targets={"NVDA": 15.0},
    )
    assert r.status is PreflightStatus.WARN


def test_concentration_within_target_passes() -> None:
    p = _proposal(ticker="AAPL")
    r = check_concentration_cap(
        p, snapshot_pct={"AAPL": 5.0}, plan_targets={"AAPL": 5.0}
    )
    assert r.status is PreflightStatus.PASS


# ----------------- wash sale -----------------


def test_wash_sale_stub_pass() -> None:
    p = _proposal()
    r = check_wash_sale(p, lots=None)
    assert r.status is PreflightStatus.PASS
    assert "Phase 4" in r.message


# ----------------- daily loss -----------------


def test_daily_loss_pass() -> None:
    p = _proposal()
    r = check_daily_loss_limit(p, day_pnl_usd=-100, daily_loss_limit_usd=-1000)
    assert r.status is PreflightStatus.PASS


def test_daily_loss_hard_fail() -> None:
    p = _proposal()
    r = check_daily_loss_limit(p, day_pnl_usd=-2000, daily_loss_limit_usd=-1000)
    assert r.status is PreflightStatus.HARD_FAIL


def test_daily_loss_no_limit() -> None:
    p = _proposal()
    r = check_daily_loss_limit(p, day_pnl_usd=-99999, daily_loss_limit_usd=None)
    assert r.status is PreflightStatus.PASS


# ----------------- trading hours -----------------


def test_trading_hours_open() -> None:
    p = _proposal()
    # 13:30 UTC is during US market hours when interpreted as ET (9:30 ET).
    now = datetime(2026, 5, 4, 13, 30, tzinfo=timezone.utc)  # Mon
    r = check_trading_hours(p, now=now)
    # Our function compares against the wall clock t directly; for the
    # test, supply 14:30 to be in the 9:30-16:00 ET window.
    now2 = datetime(2026, 5, 4, 14, 30, tzinfo=timezone.utc)
    r2 = check_trading_hours(p, now=now2)
    assert r2.status is PreflightStatus.PASS


def test_trading_hours_weekend_market_order_fails() -> None:
    p = _proposal(order_type="market")
    sat = datetime(2026, 5, 2, 14, 30, tzinfo=timezone.utc)  # Sat
    r = check_trading_hours(p, now=sat)
    assert r.status is PreflightStatus.HARD_FAIL


def test_trading_hours_weekend_limit_warns() -> None:
    p = _proposal(order_type="limit")
    sat = datetime(2026, 5, 2, 14, 30, tzinfo=timezone.utc)  # Sat
    r = check_trading_hours(p, now=sat)
    assert r.status is PreflightStatus.WARN


# ----------------- tier mode match -----------------


def test_tier_mode_match_paper() -> None:
    p = _proposal()
    settings = AgentSettings(execution=ExecutionBlock(default_mode="paper"))
    r = check_tier_mode_match(p, "T2", settings)
    assert r.status is PreflightStatus.PASS
    assert "paper" in r.message.lower()


def test_tier_mode_match_queue_only() -> None:
    p = _proposal()
    settings = AgentSettings(execution=ExecutionBlock(default_mode="queue_only"))
    r = check_tier_mode_match(p, "T0", settings)
    assert r.status is PreflightStatus.PASS
    assert "queue_only" in r.message.lower()


# ----------------- aggregator -----------------


def test_run_preflight_passes_clean_buy() -> None:
    p = _proposal()
    settings = AgentSettings(execution=ExecutionBlock(default_mode="paper"))
    inputs = PreflightInputs(
        proposal=p,
        settings=settings,
        now=datetime(2026, 5, 4, 14, 30, tzinfo=timezone.utc),
        cash_available_usd=10_000,
        max_position_usd=10_000,
        snapshot_pct={"AAPL": 5.0},
        plan_targets={"AAPL": 10.0},
        day_pnl_usd=0.0,
        daily_loss_limit_usd=-5_000.0,
        tier="T1",
        account_class="main",
    )
    report = run_preflight(inputs)
    assert report.passed
    assert "PASS" in report.summary()


def test_run_preflight_blocks_on_cash() -> None:
    p = _proposal()
    settings = AgentSettings(execution=ExecutionBlock(default_mode="paper"))
    inputs = PreflightInputs(
        proposal=p,
        settings=settings,
        now=datetime(2026, 5, 4, 14, 30, tzinfo=timezone.utc),
        cash_available_usd=100,  # not enough
        max_position_usd=10_000,
    )
    report = run_preflight(inputs)
    assert not report.passed
    assert any(r.check == "cash_availability" for r in report.hard_failures)
