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
    check_sector_concentration_cap,
    check_section102_ledger_before_sell,
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


def test_cash_none_is_input_missing_not_zero() -> None:
    """None means unavailable — must not compare against $0.00."""
    p = _proposal(action="buy", size_shares_or_currency=100, size_units="currency")
    r = check_cash_availability(p, cash_available_usd=None)
    assert r.status is PreflightStatus.HARD_FAIL
    assert r.detail.get("input_missing") is True
    assert "unavailable" in r.message.lower() or "missing" in r.message.lower()
    assert "$0.00" not in r.message


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


# ----------------- sector concentration cap -----------------

# Classification map used throughout — mirrors instrument_reference for these
# tickers so tests are self-contained and don't depend on the live reference.
_TECH_CLASS_MAP: dict[str, str] = {
    "NVDA": "Tech",
    "AMD": "Tech",
    "CRM": "Tech",
    "CRWD": "Tech",
    "CSPX": "Broad Index",
    "SGOV": "T-Bill",
    "AMZN": "Consumer Discretionary",
}

_TECH_CAPS: dict[str, float] = {"Tech": 35.0}


def test_sector_cap_buy_over_cap_hard_fails() -> None:
    """A buy into a tech ticker when tech is already at 40% must HARD_FAIL."""
    p = _proposal(ticker="NVDA", action="buy")
    snapshot = {"NVDA": 25.0, "AMD": 10.0, "CRM": 5.0, "CSPX": 30.0, "SGOV": 10.0}
    # Tech total = 25 + 10 + 5 = 40% > 35% cap
    r = check_sector_concentration_cap(p, snapshot, _TECH_CAPS, _TECH_CLASS_MAP)
    assert r.status is PreflightStatus.HARD_FAIL
    assert "Tech" in r.message
    assert r.detail.get("sector") == "Tech"
    assert r.detail.get("current_pct", 0) > 35.0


def test_sector_cap_buy_within_cap_passes() -> None:
    """A buy into a tech ticker when tech is only 20% must PASS."""
    p = _proposal(ticker="CRWD", action="buy")
    snapshot = {"NVDA": 10.0, "AMD": 10.0, "CSPX": 40.0, "SGOV": 20.0, "AMZN": 20.0}
    # Tech total = 10 + 10 = 20% < 35% cap
    r = check_sector_concentration_cap(p, snapshot, _TECH_CAPS, _TECH_CLASS_MAP)
    assert r.status is PreflightStatus.PASS


def test_sector_cap_unknown_sector_does_not_silently_pass() -> None:
    """Proposed ticker absent from classification_map must HARD_FAIL, not pass."""
    p = _proposal(ticker="UNKNOWN_TKR", action="buy")
    snapshot = {"NVDA": 10.0, "CSPX": 40.0}
    r = check_sector_concentration_cap(p, snapshot, _TECH_CAPS, _TECH_CLASS_MAP)
    assert r.status is PreflightStatus.HARD_FAIL
    assert r.detail.get("missing_classification") is True
    assert "UNKNOWN_TKR" in r.message


def test_sector_cap_no_caps_configured_passes() -> None:
    """Empty sector_caps dict → check skipped (PASS), not a block."""
    p = _proposal(ticker="NVDA", action="buy")
    snapshot = {"NVDA": 80.0}  # absurdly concentrated — but no cap configured
    r = check_sector_concentration_cap(p, snapshot, {}, _TECH_CLASS_MAP)
    assert r.status is PreflightStatus.PASS


def test_sector_cap_sell_over_cap_warns_not_blocks() -> None:
    """A sell while already over cap should WARN, not block — it reduces exposure."""
    p = _proposal(ticker="NVDA", action="sell")
    snapshot = {"NVDA": 30.0, "AMD": 15.0, "CSPX": 30.0, "SGOV": 10.0, "AMZN": 15.0}
    # Tech = 30 + 15 = 45% — over cap, but action is sell
    r = check_sector_concentration_cap(p, snapshot, _TECH_CAPS, _TECH_CLASS_MAP)
    assert r.status is PreflightStatus.WARN


def test_sector_cap_unclassified_held_ticker_hard_fails() -> None:
    """If a held ticker in snapshot_pct has no sector, the check must block."""
    p = _proposal(ticker="NVDA", action="buy")
    snapshot = {"NVDA": 10.0, "MYSTERY_CO": 20.0, "CSPX": 30.0}
    # MYSTERY_CO is not in _TECH_CLASS_MAP — sector total is incomplete
    r = check_sector_concentration_cap(p, snapshot, _TECH_CAPS, _TECH_CLASS_MAP)
    assert r.status is PreflightStatus.HARD_FAIL
    assert "MYSTERY_CO" in r.message or "unclassified" in r.message.lower()


def test_sector_cap_buy_non_capped_sector_passes() -> None:
    """Buying a non-Tech ticker (e.g. AMZN = Consumer Discretionary) always passes
    when only a Tech cap is configured."""
    p = _proposal(ticker="AMZN", action="buy")
    snapshot = {"AMZN": 25.0, "CSPX": 40.0, "SGOV": 10.0, "NVDA": 5.0, "AMD": 5.0}
    # Tech = 10%, Consumer Disc = 25% — only Tech has a cap; AMZN is Cons Disc
    r = check_sector_concentration_cap(p, snapshot, _TECH_CAPS, _TECH_CLASS_MAP)
    assert r.status is PreflightStatus.PASS


def test_sector_cap_via_run_preflight_blocks_buy() -> None:
    """run_preflight surfaces a sector cap breach as a hard_failure."""
    p = _proposal(ticker="NVDA", action="buy")
    settings = AgentSettings(execution=ExecutionBlock(default_mode="paper"))
    inputs = PreflightInputs(
        proposal=p,
        settings=settings,
        now=datetime(2026, 5, 4, 14, 30, tzinfo=timezone.utc),
        cash_available_usd=10_000,
        snapshot_pct={"NVDA": 25.0, "AMD": 15.0, "CSPX": 30.0, "SGOV": 10.0},
        sector_caps={"Tech": 35.0},
        classification_map=_TECH_CLASS_MAP,
    )
    report = run_preflight(inputs)
    assert not report.passed
    assert any(r.check == "sector_concentration_cap" for r in report.hard_failures)


# ----------------- wash sale -----------------


def test_wash_sale_stub_pass() -> None:
    p = _proposal()
    r = check_wash_sale(p, lots=None)
    assert r.status is PreflightStatus.PASS
    assert "Phase 4" in r.message


# ----------------- Section-102 ledger-before-sell (run-369 fix) -----------------
# Enforced at TRADE PREFLIGHT (not plan approval) per Ariel's ruling: the plan is
# allowed to STATE the deconcentration goal, but a SELL order for a Section-102
# ticker must not place until the per-lot eligibility ledger is loaded and covers
# the shares being sold — selling the wrong lots is an irreversible tax mistake.


def test_section102_sell_blocked_with_no_ledger() -> None:
    """No ledger loaded at all for NVDA -> HARD_FAIL, not a silent pass."""
    p = _proposal(ticker="NVDA", action="sell", size_shares_or_currency=1_000)
    r = check_section102_ledger_before_sell(p, frozenset({"NVDA"}), {})
    assert r.status is PreflightStatus.HARD_FAIL
    assert r.detail.get("ledger_loaded") is False


def test_section102_sell_allowed_when_ledger_covers_shares() -> None:
    p = _proposal(ticker="NVDA", action="sell", size_shares_or_currency=1_000)
    r = check_section102_ledger_before_sell(
        p, frozenset({"NVDA"}), {"NVDA": 9_230.0}
    )
    assert r.status is PreflightStatus.PASS


def test_section102_sell_blocked_when_ledger_covers_fewer_shares_than_requested() -> None:
    """3,924 requested vs only 1,710 verified eligible -> HARD_FAIL (would reach
    into unverified/breaking lots taxed at ~50% instead of ~30%)."""
    p = _proposal(ticker="NVDA", action="sell", size_shares_or_currency=3_924)
    r = check_section102_ledger_before_sell(
        p, frozenset({"NVDA"}), {"NVDA": 1_710.0}
    )
    assert r.status is PreflightStatus.HARD_FAIL
    assert r.detail.get("requested") == 3_924
    assert r.detail.get("eligible") == 1_710.0


def test_section102_buy_unaffected() -> None:
    """A BUY of the Section-102 ticker is untouched by the ledger check even
    with no ledger loaded."""
    p = _proposal(ticker="NVDA", action="buy", size_shares_or_currency=100)
    r = check_section102_ledger_before_sell(p, frozenset({"NVDA"}), {})
    assert r.status is PreflightStatus.PASS


def test_section102_unrelated_ticker_unaffected() -> None:
    """A SELL of a ticker with no Section-102 lots is not gated at all."""
    p = _proposal(ticker="AAPL", action="sell", size_shares_or_currency=1_000)
    r = check_section102_ledger_before_sell(p, frozenset({"NVDA"}), {})
    assert r.status is PreflightStatus.PASS


def test_section102_via_run_preflight_blocks_sell_with_no_ledger() -> None:
    """run_preflight surfaces the missing-ledger Section-102 gap as a hard failure."""
    p = _proposal(ticker="NVDA", action="sell", size_shares_or_currency=1_000)
    settings = AgentSettings(execution=ExecutionBlock(default_mode="paper"))
    inputs = PreflightInputs(
        proposal=p,
        settings=settings,
        now=datetime(2026, 5, 4, 14, 30, tzinfo=timezone.utc),
        cash_available_usd=10_000,
        section102_tickers=frozenset({"NVDA"}),
    )
    report = run_preflight(inputs)
    assert not report.passed
    assert any(r.check == "section102_ledger_before_sell" for r in report.hard_failures)


def test_section102_via_run_preflight_passes_when_ledger_covers() -> None:
    p = _proposal(ticker="NVDA", action="sell", size_shares_or_currency=1_000)
    settings = AgentSettings(execution=ExecutionBlock(default_mode="paper"))
    inputs = PreflightInputs(
        proposal=p,
        settings=settings,
        now=datetime(2026, 5, 4, 14, 30, tzinfo=timezone.utc),
        cash_available_usd=10_000,
        section102_tickers=frozenset({"NVDA"}),
        section102_eligible_shares={"NVDA": 9_230.0},
    )
    report = run_preflight(inputs)
    assert not any(
        r.check == "section102_ledger_before_sell" for r in report.hard_failures
    )


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


def test_sector_cap_tickerless_proposal_cannot_pass():
    """Cannot evaluate => cannot pass (Sol review 2026-08-13, blocker 1)."""
    from argosy.decisions.risk_preflight import (
        PreflightStatus,
        check_sector_concentration_cap,
    )

    class _P:
        ticker = ""
        action = "buy"

    r = check_sector_concentration_cap(
        _P(), {"NVDA": 58.0}, {"Tech": 35.0}, {"NVDA": "Tech"}
    )
    assert r.status is PreflightStatus.HARD_FAIL


def test_sector_cap_none_weight_is_missing_data_not_zero():
    """A None weight must not be silently treated as 0% (Sol blocker 2)."""
    from argosy.decisions.risk_preflight import (
        PreflightStatus,
        check_sector_concentration_cap,
    )

    class _P:
        ticker = "AMD"
        action = "buy"

    r = check_sector_concentration_cap(
        _P(),
        {"NVDA": None, "AMD": 2.0},
        {"Tech": 35.0},
        {"NVDA": "Tech", "AMD": "Tech"},
    )
    assert r.status is PreflightStatus.HARD_FAIL
    assert "classification" in r.message.lower() or "cannot be computed" in r.message.lower()
