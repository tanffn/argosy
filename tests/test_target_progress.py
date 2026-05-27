"""Target-progress service + route tests.

Covers each unit shape (pct_of_portfolio, usd, nis, shares, months,
ratio, unknown) with seeded snapshot + household_budget + concentration
rows. The classifier should:

  * return AT_TARGET / ABOVE_TARGET / BELOW_TARGET with correct
    direction_is_good flags
  * fall back to UNKNOWN cleanly when source data is missing
  * never raise — even on malformed payloads, classifier returns UNKNOWN

The route layer test mounts the full FastAPI app and exercises the
``/api/plan/draft/target-progress`` endpoint end-to-end.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from argosy.services.target_progress import (
    TargetProgress,
    compute_target_progress_for_plan,
)
from argosy.state.models import (
    AgentReport,
    PlanVersion,
    PortfolioSnapshotRow,
    User,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _seed_user(sess) -> None:
    if sess.get(User, "ariel") is None:
        sess.add(User(id="ariel", plan="free"))
        sess.commit()


def _seed_snapshot(
    sess,
    *,
    nvda_usd_k: float = 2296.0,
    nvda_shares: float = 11471.0,
    nvda_price: float = 200.14,
    sgov_usd_k: float = 125.0,
    cash_usd_k: float = 50.0,
    other_us_etf_usd_k: float = 250.0,
    total_usd_k: float = 4025.0,
    fx_usd_nis: float = 3.10,
) -> PortfolioSnapshotRow:
    """Insert one PortfolioSnapshotRow with deterministic, plan-style
    positions. Numbers chosen so plan #11's medium-horizon targets land
    near familiar values:
      NVDA 2,296k / 4,025k = ~57%   (target 45% → ABOVE)
      SGOV 125k                     (target 125,000 → AT_TARGET)
      US-domiciled ETFs 250k        (target 250,000 → AT_TARGET ceiling)
      NVDA shares 11,471            (target 9,059 → ABOVE_TARGET ceiling)
    """
    positions = [
        {
            "symbol": "NVDA",
            "shares": nvda_shares,
            "current_price": nvda_price,
            "usd_value_k": nvda_usd_k,
            "asset_type": "stock",
            "currency": "USD",
            "location": "schwab",
        },
        {
            "symbol": "SGOV",
            "shares": 1250,
            "usd_value_k": sgov_usd_k,
            "asset_type": "etf",
            "currency": "USD",
            "location": "schwab",
        },
        {
            "symbol": "VTI",
            "usd_value_k": other_us_etf_usd_k,
            "asset_type": "etf",
            "currency": "USD",
            "location": "schwab",
        },
        {
            "symbol": "-",
            "asset_type": "cash",
            "currency": "USD",
            "usd_value_k": cash_usd_k,
        },
    ]
    totals = {"total_usd_value_k": total_usd_k}
    row = PortfolioSnapshotRow(
        user_id="ariel",
        snapshot_date=datetime(2026, 5, 27).date(),
        imported_at=datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc),
        positions_json=json.dumps(positions),
        totals_json=json.dumps(totals),
        fx_usd_nis=fx_usd_nis,
    )
    sess.add(row)
    sess.commit()
    return row


def _seed_household_budget(sess, *, burn_nis: float = 23000.0) -> None:
    sess.add(
        AgentReport(
            user_id="ariel",
            agent_role="household_budget",
            decision_id="cadence-household-budget",
            response_text=json.dumps(
                {
                    "monthly_burn_nis": burn_nis,
                    "monthly_income_nis": 55000.0,
                    "monthly_net_nis": 55000.0 - burn_nis,
                    "confidence": "HIGH",
                }
            ),
            confidence="HIGH",
            model="claude-sonnet-4-6",
            tokens_in=10,
            tokens_out=20,
            cost_usd=0.001,
        )
    )
    sess.commit()


def _seed_concentration(sess, *, decision_run_id: int, shares_sold_ytd: int = 1600) -> None:
    payload = {
        "summary": "test",
        "nvda_pace": {
            "shares_sold_ytd": shares_sold_ytd,
            "target_shares_ytd": 1440,
            "delta_shares": shares_sold_ytd - 1440,
            "on_track": True,
        },
        "confidence": "MEDIUM",
        "cited_sources": [],
    }
    sess.add(
        AgentReport(
            user_id="ariel",
            agent_role="concentration",
            decision_id=f"plan-synth-{decision_run_id}",
            response_text=json.dumps(payload),
            confidence="MEDIUM",
            model="claude-opus-4-7",
            tokens_in=10,
            tokens_out=20,
            cost_usd=0.001,
        )
    )
    sess.commit()


def _seed_plan_with_targets(
    sess,
    *,
    decision_run_id: int | None = None,
    targets: list[dict] | None = None,
) -> PlanVersion:
    """Seed a draft plan with the 6 representative medium-horizon targets."""
    if targets is None:
        targets = [
            {
                "label": "NVDA share of portfolio (12-month target)",
                "value": 45.0,
                "unit": "pct_of_portfolio",
                "rationale": "ceiling",
            },
            {
                "label": "NVDA deconcentration shares to sell (next 12 months)",
                "value": 4500.0,
                "unit": "shares",
                "rationale": "floor",
            },
            {
                "label": "NVDA ending share-count at 12-month gate",
                "value": 9059.0,
                "unit": "shares",
                "rationale": "ceiling",
            },
            {
                "label": "Defensive cash-equivalent sleeve floor",
                "value": 125000.0,
                "unit": "usd",
                "rationale": "floor",
            },
            {
                "label": "US-domiciled ETF aggregate value (excl. NVDA) — 12-month ceiling",
                "value": 250000.0,
                "unit": "usd",
                "rationale": "ceiling",
            },
            {
                "label": "Life-insurance face amount target (NIS)",
                "value": 4000000.0,
                "unit": "nis",
                "rationale": "floor",
            },
            {
                "label": "USD/NIS hedge ratio (target)",
                "value": 45.0,
                "unit": "ratio",
                "rationale": "ratio",
            },
            {
                "label": "Defensive runway floor",
                "value": 24.0,
                "unit": "months",
                "rationale": "floor",
            },
        ]
    horizon_medium = {
        "horizon": "medium",
        "freshness_expected": "quarterly",
        "status": "minor_revision",
        "posture": "test posture",
        "targets": targets,
        "themes": [],
        "actions": [],
        "speculative_candidates": [],
        "deltas_from_prior": [],
        "rationale": "",
        "cited_sources": [],
    }
    pv = PlanVersion(
        user_id="ariel",
        role="draft",
        version_label="test-draft",
        raw_markdown="",
        horizon_medium_md="# Medium",
        horizon_medium_json=json.dumps(horizon_medium),
        decision_run_id=decision_run_id,
    )
    sess.add(pv)
    sess.commit()
    sess.refresh(pv)
    return pv


# ---------------------------------------------------------------------------
# Unit tests — pure classifier (no FastAPI)
# ---------------------------------------------------------------------------


def _index(rows: list[TargetProgress]) -> dict[str, TargetProgress]:
    return {r.item_id: r for r in rows}


def test_pct_of_portfolio_above_target_when_nvda_overweight(client_with_db):
    """NVDA at 2,296k / 4,025k = ~57% vs target 45% → ABOVE_TARGET ceiling."""
    sess = client_with_db.app.state.session_factory()
    try:
        _seed_user(sess)
        _seed_snapshot(sess)
        pv = _seed_plan_with_targets(sess)
        rows = compute_target_progress_for_plan(sess, user_id="ariel", plan=pv)
    finally:
        sess.close()

    by_id = _index(rows)
    # Slug: "nvda_share_of_portfolio__12_month_target"
    key = next(k for k in by_id if "nvda_share_of_portfolio" in k)
    r = by_id[key]
    assert r.status == "ABOVE_TARGET", f"{r.status} for {r.last_observation}"
    assert r.current_value is not None
    assert 55.0 <= r.current_value <= 60.0
    assert r.direction_is_good is False  # ceiling
    assert r.gap_value is not None and r.gap_value > 0
    assert "NVDA" in r.last_observation


def test_usd_floor_at_target_for_sgov_sleeve(client_with_db):
    """SGOV 125k + cash 50k = 175k vs floor 125k → ABOVE_TARGET, good dir."""
    sess = client_with_db.app.state.session_factory()
    try:
        _seed_user(sess)
        _seed_snapshot(sess, sgov_usd_k=125.0, cash_usd_k=50.0)
        pv = _seed_plan_with_targets(sess)
        rows = compute_target_progress_for_plan(sess, user_id="ariel", plan=pv)
    finally:
        sess.close()

    by_id = _index(rows)
    key = next(k for k in by_id if "defensive_cash" in k or "defensive" in k)
    r = by_id[key]
    assert r.status == "ABOVE_TARGET", f"{r.status} for {r.last_observation}"
    assert r.direction_is_good is True  # floor — above is good
    assert r.current_value == pytest.approx(175_000.0, rel=1e-3)


def test_usd_ceiling_at_target_for_us_domiciled_etfs(client_with_db):
    """Other US-domiciled ETFs (VTI) at 250k matches ceiling 250k → AT_TARGET."""
    sess = client_with_db.app.state.session_factory()
    try:
        _seed_user(sess)
        _seed_snapshot(sess, other_us_etf_usd_k=250.0)
        pv = _seed_plan_with_targets(sess)
        rows = compute_target_progress_for_plan(sess, user_id="ariel", plan=pv)
    finally:
        sess.close()

    by_id = _index(rows)
    key = next(k for k in by_id if "us_domiciled_etf" in k)
    r = by_id[key]
    assert r.status == "AT_TARGET", f"{r.status} for {r.last_observation}"
    assert r.direction_is_good is False  # ceiling
    assert r.current_value == pytest.approx(250_000.0, rel=1e-3)


def test_shares_to_sell_below_floor(client_with_db):
    """Concentration says 1,600 sold YTD vs target 4,500 to sell → BELOW_TARGET."""
    from argosy.state.models import DecisionRun

    sess = client_with_db.app.state.session_factory()
    try:
        _seed_user(sess)
        _seed_snapshot(sess)
        run = DecisionRun(
            user_id="ariel",
            ticker="(plan)",
            decision_kind="plan_revision",
            status="completed",
            started_at=datetime.now(timezone.utc),
        )
        sess.add(run)
        sess.commit()
        sess.refresh(run)
        _seed_concentration(sess, decision_run_id=run.id, shares_sold_ytd=1600)
        pv = _seed_plan_with_targets(sess, decision_run_id=run.id)
        rows = compute_target_progress_for_plan(sess, user_id="ariel", plan=pv)
    finally:
        sess.close()

    by_id = _index(rows)
    key = next(k for k in by_id if "deconcentration_shares" in k)
    r = by_id[key]
    assert r.status == "BELOW_TARGET", f"{r.status} for {r.last_observation}"
    assert r.direction_is_good is True  # floor — above is good
    assert r.current_value == 1600.0


def test_shares_ending_count_above_ceiling(client_with_db):
    """NVDA holdings 11,471 vs ceiling 9,059 → ABOVE_TARGET (bad direction)."""
    sess = client_with_db.app.state.session_factory()
    try:
        _seed_user(sess)
        _seed_snapshot(sess, nvda_shares=11471.0)
        pv = _seed_plan_with_targets(sess)
        rows = compute_target_progress_for_plan(sess, user_id="ariel", plan=pv)
    finally:
        sess.close()

    by_id = _index(rows)
    key = next(k for k in by_id if "ending_share_count" in k)
    r = by_id[key]
    assert r.status == "ABOVE_TARGET", f"{r.status} for {r.last_observation}"
    assert r.direction_is_good is False  # ceiling
    assert r.current_value == 11471.0


def test_nis_target_falls_back_to_unknown(client_with_db):
    """Life-insurance face amount isn't in the snapshot → UNKNOWN."""
    sess = client_with_db.app.state.session_factory()
    try:
        _seed_user(sess)
        _seed_snapshot(sess)
        pv = _seed_plan_with_targets(sess)
        rows = compute_target_progress_for_plan(sess, user_id="ariel", plan=pv)
    finally:
        sess.close()

    by_id = _index(rows)
    key = next(k for k in by_id if "life_insurance" in k)
    r = by_id[key]
    assert r.status == "UNKNOWN"
    assert r.current_value is None
    assert "life-insurance" in r.compute_source.lower()


def test_ratio_unit_falls_back_to_unknown(client_with_db):
    """USD/NIS hedge ratio isn't tracked in DB → UNKNOWN."""
    sess = client_with_db.app.state.session_factory()
    try:
        _seed_user(sess)
        _seed_snapshot(sess)
        pv = _seed_plan_with_targets(sess)
        rows = compute_target_progress_for_plan(sess, user_id="ariel", plan=pv)
    finally:
        sess.close()

    by_id = _index(rows)
    key = next(k for k in by_id if "usd_nis_hedge_ratio" in k)
    r = by_id[key]
    assert r.status == "UNKNOWN"
    assert r.direction_is_good is None


def test_months_target_uses_burn_and_defensive(client_with_db):
    """24-month runway floor — with $175k defensive (SGOV 125 + cash 50) at
    FX 3.10 and ₪30k burn, runway = (175,000 × 3.10) / 30,000 ≈ 18.1
    months. Clearly below the 24-month floor → BELOW_TARGET, direction
    good = True (a runway floor is the kind of target where above is
    good)."""
    sess = client_with_db.app.state.session_factory()
    try:
        _seed_user(sess)
        _seed_snapshot(sess, sgov_usd_k=125.0, cash_usd_k=50.0, fx_usd_nis=3.10)
        _seed_household_budget(sess, burn_nis=30000.0)
        pv = _seed_plan_with_targets(sess)
        rows = compute_target_progress_for_plan(sess, user_id="ariel", plan=pv)
    finally:
        sess.close()

    by_id = _index(rows)
    key = next(k for k in by_id if "defensive_runway" in k)
    r = by_id[key]
    assert r.current_value is not None
    assert 17.5 <= r.current_value <= 19.0, r.current_value
    assert r.status == "BELOW_TARGET", f"{r.status}"
    assert r.direction_is_good is True


def test_no_snapshot_returns_unknown(client_with_db):
    """No portfolio snapshot at all → every target UNKNOWN."""
    sess = client_with_db.app.state.session_factory()
    try:
        _seed_user(sess)
        pv = _seed_plan_with_targets(sess)
        rows = compute_target_progress_for_plan(sess, user_id="ariel", plan=pv)
    finally:
        sess.close()

    assert len(rows) >= 6
    for r in rows:
        assert r.status == "UNKNOWN", f"{r.item_id} got {r.status}"
        assert r.current_value is None


def test_classifier_never_raises_on_malformed_target(client_with_db):
    """A malformed target row should yield UNKNOWN, not crash."""
    sess = client_with_db.app.state.session_factory()
    try:
        _seed_user(sess)
        _seed_snapshot(sess)
        pv = _seed_plan_with_targets(
            sess,
            targets=[
                {"label": "Garbage", "value": None, "unit": "potatoes"},
                {"label": "No value", "value": "not-a-number", "unit": "usd"},
            ],
        )
        rows = compute_target_progress_for_plan(sess, user_id="ariel", plan=pv)
    finally:
        sess.close()
    assert len(rows) == 2
    for r in rows:
        assert r.status == "UNKNOWN"


# ---------------------------------------------------------------------------
# Route-level test — GET /api/plan/draft/target-progress
# ---------------------------------------------------------------------------


def test_route_returns_progress_map_for_pending_draft(client_with_db):
    sess = client_with_db.app.state.session_factory()
    try:
        _seed_user(sess)
        _seed_snapshot(sess)
        _seed_plan_with_targets(sess)
    finally:
        sess.close()

    r = client_with_db.get(
        "/api/plan/draft/target-progress?user_id=ariel"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan_version_id"] is not None
    assert isinstance(body["progress"], dict)
    # At least the 8 seeded targets should be in the map.
    assert len(body["progress"]) >= 6
    # NVDA pct target ought to be present + classified ABOVE_TARGET.
    nvda_keys = [
        k for k in body["progress"] if "nvda_share_of_portfolio" in k
    ]
    assert nvda_keys, list(body["progress"].keys())
    row = body["progress"][nvda_keys[0]]
    assert row["status"] == "ABOVE_TARGET"
    assert row["direction_is_good"] is False
    assert row["current_value"] is not None


def test_route_404_when_no_pending_draft(client_with_db):
    r = client_with_db.get(
        "/api/plan/draft/target-progress?user_id=newcomer"
    )
    assert r.status_code == 404
