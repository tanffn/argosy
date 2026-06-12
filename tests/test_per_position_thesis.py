"""Unit tests for argosy.services.per_position_thesis (T4.1).

Covers the four required scenarios from the plan:

  * NVDA at 64.9% with a 12-month target of 45% → TRIM (not SELL —
    target reduces weight by ~30%, below the 50% SELL threshold).
  * NVDA at 64.9% with a target of 10% → SELL (>50% reduction).
  * SGOV at floor → HOLD (no targets/actions name a change; the only
    mention is a floor preservation rationale).
  * UCITS replacement candidate (XEON) NOT in current portfolio →
    appears as an ADD card.
  * Ticker with empty analyst data → HOLD verdict + LOW conviction.

Plus a couple of structural tests for the FastAPI route to verify
the wiring (404 → empty list contract + happy path shape).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from argosy.services.per_position_thesis import (
    PositionThesis,
    derive_position_theses,
)


# ---------------------------------------------------------------------------
# Test fixtures — synthetic plan + portfolio + analyst reports.
# ---------------------------------------------------------------------------


def _make_position(symbol: str, shares: float, usd_value_k: float) -> dict:
    return {
        "symbol": symbol,
        "shares": shares,
        "usd_value_k": usd_value_k,
    }


def _make_horizon(
    horizon: str,
    targets: list[dict] | None = None,
    actions: list[dict] | None = None,
) -> str:
    """JSON-serialize a horizon payload in the same shape the synthesizer emits."""
    return json.dumps({
        "horizon": horizon,
        "freshness_expected": "monthly",
        "status": "minor_revision",
        "posture": "test",
        "targets": targets or [],
        "themes": [],
        "actions": actions or [],
        "speculative_candidates": [],
        "deltas_from_prior": [],
        "rationale": "",
        "cited_sources": [],
    })


def _plan_version(
    horizon_short: str | None = None,
    horizon_medium: str | None = None,
    horizon_long: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        horizon_short_json=horizon_short,
        horizon_medium_json=horizon_medium,
        horizon_long_json=horizon_long,
    )


def _portfolio(positions: list[dict]) -> SimpleNamespace:
    total_usd_k = sum(p.get("usd_value_k") or 0.0 for p in positions)
    return SimpleNamespace(
        positions=positions,
        total_usd_value_k=total_usd_k,
    )


def _agent_report(
    role: str,
    response_text: str,
    confidence: str | None = "MEDIUM",
    sources_json: list[dict] | None = None,
) -> dict:
    return {
        "agent_role": role,
        "response_text": response_text,
        "confidence": confidence,
        "sources_json": json.dumps(sources_json) if sources_json else None,
    }


# ---------------------------------------------------------------------------
# Verdict classification
# ---------------------------------------------------------------------------


def test_nvda_over_weight_classified_as_trim_for_moderate_target():
    """NVDA at 64.9% with a 45% target → TRIM (delta ~30% < 50% SELL line)."""
    positions = [
        _make_position("NVDA", 11471, 6490.0),   # $6.49M @ 64.9% if total=$10M
        _make_position("SGOV", 1250, 125.0),
        _make_position("SCHD", 100, 3385.0),
    ]
    horizon_med = _make_horizon(
        "medium",
        targets=[{
            "label": "NVDA share of portfolio (12-month target)",
            "value": 45.0,
            "unit": "pct_of_portfolio",
            "rationale": "ConcentrationAnalyst confirms current weight at 64.9%.",
            "source_section": "medium.targets",
        }],
        actions=[{
            "label": "Continue NVDA single-name deconcentration toward 15% long-horizon cap",
            "horizon_kind": "directional",
            "detail": "Sustain the 10,000-share annual sell-down.",
            "rationale": "Bear case won the long debate.",
            "cited_sources": ["agent_report:ConcentrationAnalystAgent"],
        }],
    )
    pv = _plan_version(horizon_medium=horizon_med)
    snap = _portfolio(positions)
    reports = [
        _agent_report(
            "concentration",
            "NVDA single-name risk dominates the portfolio at 64.9%.",
            confidence="HIGH",
            sources_json=[{"source_id": "portfolio/holdings", "content": "NVDA 11471 sh"}],
        ),
    ]

    out = derive_position_theses(pv, snap, reports)
    nvda = next(c for c in out if c.ticker == "NVDA")
    assert nvda.verdict == "TRIM"
    assert nvda.target_weight_pct == 45.0
    # Conviction picks up HIGH from the concentration analyst row.
    assert nvda.conviction == "HIGH"
    # Cited sources include the portfolio/holdings reference.
    assert any("portfolio/holdings" in s for s in nvda.cited_sources)


def test_nvda_large_reduction_classified_as_sell():
    """NVDA at 64.9% with a 10% target → SELL (>50% reduction)."""
    positions = [_make_position("NVDA", 11471, 6490.0)]
    horizon_long = _make_horizon(
        "long",
        targets=[{
            "label": "NVDA share of portfolio (long-horizon ceiling)",
            "value": 10.0,
            "unit": "pct_of_portfolio",
            "rationale": "Strategic deconcentration target.",
        }],
    )
    pv = _plan_version(horizon_long=horizon_long)
    snap = _portfolio(positions)
    out = derive_position_theses(pv, snap, [])
    nvda = next(c for c in out if c.ticker == "NVDA")
    assert nvda.verdict == "SELL"
    assert nvda.target_weight_pct == 10.0


def test_sgov_at_floor_classified_as_hold():
    """SGOV at $125k with a preserved floor of $125k → HOLD.

    No weight target is given (the target is in USD, not pct), and the
    rationale explicitly says "preserved". The classifier should
    fall through to HOLD because:
      * the SGOV target's unit is "usd" (we don't recognize that as a
        weight target),
      * no action label/detail says trim/sell/buy/add for SGOV.
    """
    positions = [
        _make_position("SGOV", 1250, 125.0),
        _make_position("NVDA", 100, 50.0),  # filler so total != SGOV alone
    ]
    horizon_med = _make_horizon(
        "medium",
        targets=[{
            "label": "Defensive SGOV sleeve floor",
            "value": 125000.0,
            "unit": "usd",
            "rationale": "Preserved. Current SGOV holdings ~$125k aggregate.",
        }],
    )
    pv = _plan_version(horizon_medium=horizon_med)
    snap = _portfolio(positions)
    out = derive_position_theses(pv, snap, [])
    sgov = next(c for c in out if c.ticker == "SGOV")
    assert sgov.verdict == "HOLD"


def test_ucits_replacement_xeon_appears_as_add_card():
    """XEON is named in an action but not in the portfolio → ADD card."""
    positions = [
        _make_position("NVDA", 100, 100.0),
        _make_position("VOO", 20, 13.0),
    ]
    horizon_short = _make_horizon(
        "short",
        actions=[{
            "label": "Liquidate VOO; redeploy to UCITS XEON sleeve",
            "horizon_kind": "dated",
            "detail": "VOO 20 sh ($13k) → XEON for euro-denominated cash equivalent.",
            "rationale": "PlanCritique Findings 3 + 4 RED — UCITS replacement.",
        }],
    )
    pv = _plan_version(horizon_short=horizon_short)
    snap = _portfolio(positions)
    out = derive_position_theses(pv, snap, [])

    # XEON should appear as an ADD card (not in portfolio).
    xeon = next((c for c in out if c.ticker == "XEON"), None)
    assert xeon is not None, "Expected XEON ADD card; got %r" % [c.ticker for c in out]
    assert xeon.verdict == "ADD"
    assert xeon.current_shares is None
    assert xeon.current_usd_value is None
    # Conviction with zero analyst data is LOW per spec.
    assert xeon.conviction == "LOW"


def test_ticker_with_empty_analyst_data_holds_with_low_conviction():
    """No analyst mentions of a held ticker → HOLD + LOW conviction."""
    positions = [_make_position("MSFT", 50, 200.0)]
    horizon_med = _make_horizon("medium", targets=[], actions=[])
    pv = _plan_version(horizon_medium=horizon_med)
    snap = _portfolio(positions)
    out = derive_position_theses(pv, snap, [])
    msft = next(c for c in out if c.ticker == "MSFT")
    assert msft.verdict == "HOLD"
    assert msft.conviction == "LOW"
    assert msft.cited_sources == []


def test_sort_order_held_first_then_add_cards():
    """Held cards (sorted by USD value desc) precede ADD cards."""
    positions = [
        _make_position("NVDA", 100, 600.0),
        _make_position("SCHD", 50, 200.0),
    ]
    horizon_short = _make_horizon(
        "short",
        actions=[{
            "label": "Open CSPX position",
            "detail": "Add CSPX as core UCITS holding.",
            "rationale": "UCITS migration.",
        }],
    )
    pv = _plan_version(horizon_short=horizon_short)
    snap = _portfolio(positions)
    out = derive_position_theses(pv, snap, [])
    # First two are the held tickers (sorted by USD desc — NVDA before SCHD).
    held = [c for c in out if c.verdict != "ADD"]
    add = [c for c in out if c.verdict == "ADD"]
    assert held[0].ticker == "NVDA"
    assert held[1].ticker == "SCHD"
    # ADD card(s) appear after.
    add_idx = out.index(add[0])
    held_idx = out.index(held[-1])
    assert add_idx > held_idx


def test_conviction_majority_vote():
    """Two HIGH analyst rows outvoting one MEDIUM → HIGH conviction."""
    positions = [_make_position("AAPL", 10, 20.0)]
    pv = _plan_version(horizon_medium=_make_horizon("medium"))
    reports = [
        _agent_report("fundamentals", "AAPL trades at a premium multiple.", "HIGH"),
        _agent_report("technical", "AAPL RSI is 55, MACD neutral.", "HIGH"),
        _agent_report("news", "AAPL announced a new product.", "MEDIUM"),
    ]
    out = derive_position_theses(pv, _portfolio(positions), reports)
    aapl = next(c for c in out if c.ticker == "AAPL")
    # 2 HIGH > 1 MEDIUM + 0 LOW → HIGH
    assert aapl.conviction == "HIGH"


def test_reasoning_capped_at_500_chars():
    """Long rationale text is truncated to ~500 chars."""
    long_rationale = "NVDA " + ("very important detail " * 80)
    positions = [_make_position("NVDA", 100, 100.0)]
    horizon_med = _make_horizon(
        "medium",
        targets=[{
            "label": "NVDA",
            "value": 50.0,
            "unit": "pct_of_portfolio",
            "rationale": long_rationale,
        }],
    )
    pv = _plan_version(horizon_medium=horizon_med)
    snap = _portfolio(positions)
    out = derive_position_theses(pv, snap, [])
    nvda = next(c for c in out if c.ticker == "NVDA")
    assert len(nvda.reasoning_md) <= 501  # 500 chars + the "…" ellipsis


def test_reasoning_skips_raw_indicator_json_blobs():
    """A technical analyst that dumps raw indicator JSON into response_text must
    NOT leak that machine-data verbatim into the card's reasoning (the unreadable
    'SCHD … "rsi_14": 54.62 … "yfinance:RKT:1d"]' bug). Prose rationale survives;
    the raw-data excerpt is dropped."""
    positions = [_make_position("SCHD", 100, 260.0)]
    horizon_long = _make_horizon(
        "long",
        actions=[{
            "label": "Migrate SCHD to a UCITS dividend twin",
            "detail": "Swap to FUSA for estate-tax domicile; SCHD thesis intact.",
            "rationale": "Domicile swap, not a momentum sell.",
        }],
    )
    raw_blob = (
        '{"SCHD": {"ticker": "SCHD", "indicators": {"rsi_14": 54.62, '
        '"macd": 0.22, "macd_signal": 0.27, "ma_50": 31.60, "price": 32.34}, '
        '"sources": ["yfinance:SCHD:1d", "indicators/SCHD"]}}'
    )
    pv = _plan_version(horizon_long=horizon_long)
    snap = _portfolio(positions)
    reports = [_agent_report("technical", raw_blob, confidence="LOW")]
    out = derive_position_theses(pv, snap, reports)
    schd = next(c for c in out if c.ticker == "SCHD")
    # No raw-data tokens leaked.
    for needle in ('"rsi_14"', "macd_signal", "yfinance:", '"indicators"', "}}"):
        assert needle not in schd.reasoning_md, f"raw data {needle!r} leaked into card"
    # The human-readable plan rationale still made it through.
    assert "domicile" in schd.reasoning_md.lower() or "ucits" in schd.reasoning_md.lower()


def test_held_us_domiciled_etf_gets_domicile_swap_note():
    """A held US-domiciled ETF (SCHD) the plan replaces with a UCITS twin must be
    framed as an estate-tax DOMICILE swap — not a momentum/fundamental sell —
    even when the horizon JSON carries no explicit text about it. The note names
    the UCITS twin so the user understands SCHD itself is still sound."""
    positions = [_make_position("SCHD", 100, 260.0)]
    pv = _plan_version()  # no horizon mentions at all
    snap = _portfolio(positions)
    out = derive_position_theses(pv, snap, [])
    schd = next(c for c in out if c.ticker == "SCHD")
    low = schd.reasoning_md.lower()
    assert "ucits" in low and "fusa" in low
    assert "estate" in low or "domicile" in low
    # It is framed as a domicile swap that preserves exposure, explicitly NOT a
    # momentum/fundamental sell.
    assert "preserves" in low and "not a momentum or fundamental sell" in low


def test_action_with_buy_cue_classifies_buy():
    """An action that says "accumulate" → BUY verdict for a held ticker."""
    positions = [_make_position("CSPX", 5, 10.0)]
    horizon_short = _make_horizon(
        "short",
        actions=[{
            "label": "Accumulate CSPX core sleeve",
            "detail": "Build CSPX up to 25% of UCITS bucket.",
            "rationale": "UCITS core continues.",
        }],
    )
    pv = _plan_version(horizon_short=horizon_short)
    snap = _portfolio(positions)
    out = derive_position_theses(pv, snap, [])
    cspx = next(c for c in out if c.ticker == "CSPX")
    assert cspx.verdict == "BUY"


# ---------------------------------------------------------------------------
# Route smoke test — exercises the wiring via the FastAPI test client.
# ---------------------------------------------------------------------------


def test_route_returns_empty_list_when_no_plan(client_with_db):
    """No plan + no draft → empty list (NOT 404)."""
    r = client_with_db.get("/api/positions/thesis?user_id=nobody")
    assert r.status_code == 200
    assert r.json() == []


def test_route_returns_cards_from_draft(client_with_db):
    """Pending draft + (mocked) positions → cards with verdict + conviction."""
    from argosy.state.models import AgentReport, PlanVersion, User

    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, "ariel") is None:
            sess.add(User(id="ariel", plan="free"))
        sess.add(PlanVersion(user_id="ariel", role="baseline", raw_markdown=""))
        sess.add(PlanVersion(
            user_id="ariel",
            role="draft",
            version_label="t41-test",
            raw_markdown="",
            horizon_medium_json=_make_horizon(
                "medium",
                targets=[{
                    "label": "NVDA share of portfolio",
                    "value": 30.0,
                    "unit": "pct_of_portfolio",
                    "rationale": "Test target",
                }],
            ),
        ))
        sess.commit()
    finally:
        sess.close()

    # Patch the snapshot loader so we don't depend on filesystem state.
    import argosy.api.routes.positions as positions_route

    def fake_snapshot(_uid: str, _db=None):
        return SimpleNamespace(
            positions=[
                {"symbol": "NVDA", "shares": 100, "usd_value_k": 50.0},
                {"symbol": "SGOV", "shares": 1, "usd_value_k": 50.0},
            ],
            total_usd_value_k=100.0,
        )

    orig = positions_route._load_portfolio_snapshot
    positions_route._load_portfolio_snapshot = fake_snapshot
    try:
        r = client_with_db.get("/api/positions/thesis?user_id=ariel")
    finally:
        positions_route._load_portfolio_snapshot = orig

    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list)
    # NVDA is 50% of the portfolio, target is 30% (-40% relative move) →
    # below the 50% SELL threshold → TRIM.
    nvda = next(c for c in body if c["ticker"] == "NVDA")
    assert nvda["verdict"] == "TRIM"
    assert nvda["target_weight_pct"] == 30.0
    assert nvda["conviction"] == "LOW"  # no analyst reports seeded


# ---------------------------------------------------------------------------
# Dataclass round-trip
# ---------------------------------------------------------------------------


def test_position_thesis_to_dict_is_json_safe():
    t = PositionThesis(
        ticker="NVDA",
        current_shares=100.0,
        current_weight_pct=12.5,
        current_usd_value=50_000.0,
        verdict="HOLD",
        conviction="MEDIUM",
        reasoning_md="…",
        cited_sources=["portfolio/holdings"],
        target_weight_pct=15.0,
        target_shares=None,
    )
    d = t.to_dict()
    # Must round-trip through json.dumps without raising.
    json.dumps(d)
    assert d["ticker"] == "NVDA"
    assert d["verdict"] == "HOLD"
