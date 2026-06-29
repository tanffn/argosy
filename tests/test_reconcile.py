"""The reconciliation gate: ingested snapshot vs the raw Leumi source.
Internal consistency is not correctness — this catches a dropped cash
currency, a missing position, or a symbol collision."""
from __future__ import annotations

from types import SimpleNamespace

from argosy.services.portfolio_ingest.parsers.leumi_xls import LeumiPortfolioPosition
from argosy.services.portfolio_ingest.reconcile import reconcile_leumi_against_xls


def _snap(location, currency, asset_type, symbol, shares, usd_k):
    return SimpleNamespace(location=location, currency=currency, asset_type=asset_type,
                           symbol=symbol, shares=shares, usd_value_k=usd_k)


def _xp(ticker, name_he, qty, value, currency="USD"):
    """A real LeumiPortfolioPosition so the reconciler exercises its actual
    `usd_value` conversion. ``value`` is in ``currency`` (USD or NIS)."""
    return LeumiPortfolioPosition(
        security_id="x", name_he=name_he, ticker=ticker, avg_buy_price=None,
        quantity=qty, last_price=0.0, holding_value=value,
        holding_value_currency=currency, gain_pct=None, pct_of_portfolio=None,
    )


def _clean_snapshot():
    return [
        _snap("Leumi", "NIS", "Cash", "", None, 20.04),
        _snap("Leumi", "USD", "Cash", "", None, 265.0),
        _snap("Leumi", "USD", "Equity", "VOO", 20, 13.56),
        _snap("Leumi", "USD", "Equity", "STOXX Europe 600", 12500, 6.81),
    ]


def _xls_positions():
    return [
        _xp("VOO", "(ואנגארד S&P 500) VOO", 20, 13564.6),
        _xp(None, "אי בי אי מחקה STOXX Europe 600", 12500, 6810.05),
    ]


def test_clean_reconciliation_no_issues():
    issues = reconcile_leumi_against_xls(
        snapshot_positions=_clean_snapshot(), xls_positions=_xls_positions(),
        osh_closing_nis=58944.86, usd_closing=264997.33, fx_usd_nis=3.7,
    )
    assert issues == [], issues


def test_missing_usd_cash_flagged():
    snap = [p for p in _clean_snapshot() if not (p.currency == "USD" and p.asset_type == "Cash")]
    issues = reconcile_leumi_against_xls(
        snapshot_positions=snap, xls_positions=_xls_positions(),
        osh_closing_nis=58944.86, usd_closing=264997.33, fx_usd_nis=3.7,
    )
    assert any("USD cash row MISSING" in i for i in issues)


def test_symbol_collision_flagged():
    # Both the STOXX tracker and Realty Income mislabeled "O" (distinct qtys).
    snap = [
        _snap("Leumi", "USD", "Equity", "O", 12500, 6.81),
        _snap("Leumi", "USD", "REIT", "O", 300, 18.57),
    ]
    xls = [
        _xp(None, "אי בי אי מחקה STOXX Europe 600", 12500, 6810.05),
        _xp("O", "(ריאלטי אינקם) O", 300, 18570.0),
    ]
    issues = reconcile_leumi_against_xls(
        snapshot_positions=snap, xls_positions=xls,
        osh_closing_nis=None, usd_closing=None, fx_usd_nis=3.7,
    )
    assert any("collision" in i.lower() and "'O'" in i for i in issues)


def test_missing_position_flagged():
    snap = [_snap("Leumi", "USD", "Equity", "VOO", 20, 13.56)]  # STOXX dropped
    issues = reconcile_leumi_against_xls(
        snapshot_positions=snap, xls_positions=_xls_positions(),
        osh_closing_nis=None, usd_closing=None, fx_usd_nis=3.7,
    )
    assert any("not found in snapshot" in i for i in issues)


def test_nis_xls_value_converted_to_usd_for_comparison():
    # Post-2026 NIS export: VOO holding value ₪50,200 at FX 3.7 == $13,567 ≈
    # the snapshot's $13.56K. The reconciler must convert before comparing,
    # otherwise a clean snapshot would falsely flag every position.
    snap = [_snap("Leumi", "USD", "Equity", "VOO", 20, 13.567)]
    xls = [_xp("VOO", "(ואנגארד S&P 500) VOO", 20, 50200.0, currency="NIS")]
    issues = reconcile_leumi_against_xls(
        snapshot_positions=snap, xls_positions=xls,
        osh_closing_nis=None, usd_closing=None, fx_usd_nis=3.7,
    )
    assert issues == [], issues
