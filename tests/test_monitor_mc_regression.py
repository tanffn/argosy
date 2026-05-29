"""Tests for the monitor agent's MC-regression trigger (spec §5.1.2).

Sprint commit #12. Covers the baseline-anchor + diff-and-fire contract,
severity bands, and the acknowledged-baseline edge case.

The detector runs a real Monte-Carlo internally. To keep the tests
fast + deterministic, we seed minimal household state (so the MC
projection doesn't crash) and then directly mutate the prior persisted
``curr_p_solvent`` on the monitor_flags row before the second call.
That way each test exercises the diff math (curr - prev) against a
known prior value without depending on the exact stochastic output of
the MC engine itself (which is exercised in tests/test_cashflow_projection
and tests/test_retirement_ruin_probability).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import sqlalchemy as sa
import yaml
from sqlalchemy.orm import sessionmaker

from argosy.config import get_settings
from argosy.services.plan_monitor import (
    DEFAULT_MC_DELTA_PP_THRESHOLD,
    MCRegressionFlag,
    check_mc_regression,
    get_active_mc_regression_flags,
)
from argosy.state.models import (
    AgentReport,
    MonitorFlag,
    PortfolioSnapshotRow,
    UserContext,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session_factory():
    """Sessionmaker pointed at the active ARGOSY_HOME DB (set by fixture)."""
    engine = sa.create_engine(f"sqlite:///{get_settings().db_file}")
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed_minimal_user(session, *, user_id: str = "ariel") -> None:
    """Seed just enough state for project_monte_carlo not to crash.

    ``extract_household_state`` + ``extract_pension_state`` need:
      - UserContext with a date_of_birth (so current_age_years is sane)
      - a PortfolioSnapshotRow with totals_json so portfolio_value > 0
      - an AgentReport(household_budget) so monthly_burn_nis > 0

    The fixture pre-seeds ``User(id='ariel')``; we just attach the
    auxiliary rows.
    """
    identity = {
        "date_of_birth": "1982-08-28",
        "clal_pension_salary_basis_monthly_nis": 27101,
        "clal_pension_employee_pct": 6.0,
        "clal_pension_employer_pct": 6.5,
        "clal_pension_severance_pct": 8.33,
        "pensions_ariel": {
            "pension_nis": 800_000,
            "executive_insurance_nis": 750_000,
            "keren_hishtalmut_nis": 380_000,
            "provident_fund_nis": 75_000,
        },
        "pensions": {
            "keren_hishtalmut": {
                "contribution_rate_pct": 2.5,
                "employer_match_pct": 7.5,
            },
        },
        "fx_rate": {"usd_nis": 3.6},
    }
    session.add(UserContext(
        user_id=user_id,
        identity_yaml=yaml.safe_dump(identity),
        goals_yaml="",
        constraints_yaml="",
        current_stage="complete",
    ))
    session.add(PortfolioSnapshotRow(
        user_id=user_id,
        snapshot_date=date(2026, 5, 1),
        imported_at=datetime.now(timezone.utc),
        source_path="/tmp/seed.tsv",
        positions_json="[]",
        allocations_json="[]",
        nvda_sales_json="[]",
        real_estate_json="[]",
        pensions_json="[]",
        totals_json=json.dumps({"total_usd_value_k": 1500.0}),
        fx_usd_nis=3.6,
        fx_usd_eur=4.0,
        parse_warnings_json="[]",
    ))
    body = {
        "runway_class": "comfortable",
        "monthly_burn_nis": 23_000.0,
        "monthly_income_nis": 54_000.0,
        "monthly_net_nis": 31_000.0,
        "safe_withdrawal_monthly_usd": 11_000.0,
        "headroom_summary": "seeded",
        "key_concerns": [],
        "confidence": "MEDIUM",
        "cited_sources": [],
    }
    session.add(AgentReport(
        user_id=user_id, agent_role="household_budget", decision_id=None,
        prompt_hash="x", response_text=f"```json\n{json.dumps(body)}\n```",
        tokens_in=0, tokens_out=0, cost_usd=0, model="seed",
    ))
    session.commit()


def _set_prior_curr_p_solvent(
    session, *, user_id: str, prior_value: float,
) -> MonitorFlag:
    """Mutate the most-recent mc_regression row's curr_p_solvent so the
    next ``check_mc_regression`` call diffs against ``prior_value``.

    Returns the mutated row for further assertions (e.g. ack-test)."""
    row = (
        session.query(MonitorFlag)
        .filter(MonitorFlag.user_id == user_id)
        .filter(MonitorFlag.kind == "mc_regression")
        .order_by(MonitorFlag.surfaced_at.desc())
        .first()
    )
    assert row is not None, "expected at least one prior mc_regression row"
    payload = json.loads(row.payload)
    payload["curr_p_solvent"] = prior_value
    row.payload = json.dumps(payload)
    # Stamp the prior row a month earlier so surfaced_at ordering
    # cleanly puts it BEFORE the next run's row.
    row.surfaced_at = datetime.now(timezone.utc) - timedelta(days=30)
    session.commit()
    return row


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_first_run_persists_baseline_and_returns_no_flag(argosy_home_db):
    """First call for a user: persists baseline anchor, no flag fires."""
    SF = _session_factory()
    sess = SF()
    try:
        _seed_minimal_user(sess)
        result = check_mc_regression(sess, "ariel", n_paths=200, seed=42)

        assert result.flag_fired is None
        assert result.prev_run_date is None
        assert result.rows_evaluated == 1
        # Exactly one mc_regression row, payload['baseline']==True.
        rows = sess.query(MonitorFlag).filter(
            MonitorFlag.kind == "mc_regression",
        ).all()
        assert len(rows) == 1
        payload = json.loads(rows[0].payload)
        assert payload["baseline"] is True
        assert payload["fired"] is False
        assert payload["prev_p_solvent"] is None
        # Recorded curr matches what the result reports.
        assert payload["curr_p_solvent"] == round(result.curr_p_solvent, 4)
    finally:
        sess.close()


def test_second_run_no_regression_persists_anchor_no_flag(argosy_home_db):
    """Second run with P(solvent) flat: no flag fires, anchor advances."""
    SF = _session_factory()
    sess = SF()
    try:
        _seed_minimal_user(sess)
        # First run — baseline.
        first = check_mc_regression(sess, "ariel", n_paths=200, seed=42)
        # Pin the prior anchor to equal the curr we just computed — so
        # the second call's delta is ~0.
        _set_prior_curr_p_solvent(
            sess, user_id="ariel", prior_value=first.curr_p_solvent,
        )

        result = check_mc_regression(sess, "ariel", n_paths=200, seed=42)
        assert result.flag_fired is None
        # Anchor row still got written (non-baseline, fired=False).
        # Now 2 rows total: baseline + no-fire anchor.
        rows = sess.query(MonitorFlag).filter(
            MonitorFlag.kind == "mc_regression",
        ).all()
        assert len(rows) == 2
        latest = max(rows, key=lambda r: r.surfaced_at)
        payload = json.loads(latest.payload)
        assert payload["baseline"] is False
        assert payload["fired"] is False
    finally:
        sess.close()


def test_small_regression_below_threshold_does_not_fire(argosy_home_db):
    """Second run with a -2pp drop: below the 5pp threshold, no fire."""
    SF = _session_factory()
    sess = SF()
    try:
        _seed_minimal_user(sess)
        first = check_mc_regression(sess, "ariel", n_paths=200, seed=42)
        # Pin prior to (curr + 0.02) so the next run shows a -2pp drop.
        _set_prior_curr_p_solvent(
            sess, user_id="ariel", prior_value=first.curr_p_solvent + 0.02,
        )

        result = check_mc_regression(sess, "ariel", n_paths=200, seed=42)
        assert result.flag_fired is None
        # No active fired flags surface.
        active = get_active_mc_regression_flags(sess, "ariel")
        assert active == []
    finally:
        sess.close()


def test_threshold_crossing_regression_fires_with_info_severity(argosy_home_db):
    """Second run with a -6pp drop: just over 5pp threshold, info band."""
    SF = _session_factory()
    sess = SF()
    try:
        _seed_minimal_user(sess)
        first = check_mc_regression(sess, "ariel", n_paths=200, seed=42)
        # Pin prior to (curr + 0.06) -> next call's delta_pp = -6.0.
        _set_prior_curr_p_solvent(
            sess, user_id="ariel", prior_value=first.curr_p_solvent + 0.06,
        )

        result = check_mc_regression(sess, "ariel", n_paths=200, seed=42)
        assert result.flag_fired is not None
        flag = result.flag_fired
        assert isinstance(flag, MCRegressionFlag)
        # -6pp is in [-7.5, -5.0) -> 'info'.
        assert flag.severity == "info"
        assert flag.delta_pp < -DEFAULT_MC_DELTA_PP_THRESHOLD
        assert flag.delta_pp > -DEFAULT_MC_DELTA_PP_THRESHOLD * 1.5
        # Surfaces via the active-flags accessor.
        active = get_active_mc_regression_flags(sess, "ariel")
        assert len(active) == 1
        assert active[0].severity == "info"
    finally:
        sess.close()


def test_severe_regression_fires_with_critical_severity(argosy_home_db):
    """Second run with a -15pp drop: well past 2.5*threshold, critical."""
    SF = _session_factory()
    sess = SF()
    try:
        _seed_minimal_user(sess)
        first = check_mc_regression(sess, "ariel", n_paths=200, seed=42)
        # Pin prior to (curr + 0.15) -> next call delta_pp = -15.0, which
        # is < -threshold * 2.5 = -12.5  -> critical.
        target_prior = min(1.0, first.curr_p_solvent + 0.15)
        _set_prior_curr_p_solvent(
            sess, user_id="ariel", prior_value=target_prior,
        )

        result = check_mc_regression(sess, "ariel", n_paths=200, seed=42)
        assert result.flag_fired is not None
        flag = result.flag_fired
        # delta is curr - prior; we pinned prior s.t. drop is ~15pp.
        assert flag.delta_pp <= -DEFAULT_MC_DELTA_PP_THRESHOLD * 2.5 + 0.5
        # Severity classification is based on the actual delta we just
        # computed; for -15pp it's 'critical'.
        if flag.delta_pp < -DEFAULT_MC_DELTA_PP_THRESHOLD * 2.5:
            assert flag.severity == "critical"
        else:
            # If clamping reduced the gap (curr+0.15 > 1.0), severity
            # may land in 'warning' instead — still a fired flag.
            assert flag.severity in ("warning", "critical")
    finally:
        sess.close()


def test_acknowledged_baseline_does_not_prevent_future_flags(argosy_home_db):
    """User dismissing the baseline row should NOT break the next month's
    diff. Acknowledged rows still serve as comparison anchors."""
    SF = _session_factory()
    sess = SF()
    try:
        _seed_minimal_user(sess)
        first = check_mc_regression(sess, "ariel", n_paths=200, seed=42)

        # User acknowledges the baseline.
        baseline_row = (
            sess.query(MonitorFlag)
            .filter(MonitorFlag.kind == "mc_regression")
            .first()
        )
        assert baseline_row is not None
        baseline_row.acknowledged_at = datetime.now(timezone.utc)
        sess.commit()

        # Pin the (acknowledged) baseline so next run sees a -8pp drop.
        _set_prior_curr_p_solvent(
            sess, user_id="ariel", prior_value=first.curr_p_solvent + 0.08,
        )
        # Re-acknowledge (mutation refreshed the row above; the helper
        # touches surfaced_at + payload only — preserve ack stamp).
        baseline_row = sess.query(MonitorFlag).filter(
            MonitorFlag.id == baseline_row.id,
        ).first()
        assert baseline_row is not None
        if baseline_row.acknowledged_at is None:
            baseline_row.acknowledged_at = datetime.now(timezone.utc)
            sess.commit()

        result = check_mc_regression(sess, "ariel", n_paths=200, seed=42)
        # Flag still fires despite the prior anchor being acknowledged.
        assert result.flag_fired is not None
        assert result.flag_fired.delta_pp < -DEFAULT_MC_DELTA_PP_THRESHOLD
    finally:
        sess.close()


def test_two_sequential_flags_diff_against_latest_anchor(argosy_home_db):
    """Bonus: two regressions in sequence — the second compares against
    the first fired flag, not the original baseline."""
    SF = _session_factory()
    sess = SF()
    try:
        _seed_minimal_user(sess)
        first = check_mc_regression(sess, "ariel", n_paths=200, seed=42)

        # Engineer flag #1: -7pp drop from the baseline.
        _set_prior_curr_p_solvent(
            sess, user_id="ariel", prior_value=first.curr_p_solvent + 0.07,
        )
        r1 = check_mc_regression(sess, "ariel", n_paths=200, seed=42)
        assert r1.flag_fired is not None
        # The newly-fired row is now the latest mc_regression row.
        # Confirm its payload.fired==True.
        latest_after_first_fire = (
            sess.query(MonitorFlag)
            .filter(MonitorFlag.kind == "mc_regression")
            .order_by(MonitorFlag.surfaced_at.desc())
            .first()
        )
        assert latest_after_first_fire is not None
        p1 = json.loads(latest_after_first_fire.payload)
        assert p1["fired"] is True

        # Engineer flag #2: pin the just-fired row so the next call diffs
        # against IT (not the original baseline). Use a tiny prior so the
        # current curr_p_solvent makes a fresh drop.
        _set_prior_curr_p_solvent(
            sess, user_id="ariel", prior_value=r1.curr_p_solvent + 0.10,
        )
        r2 = check_mc_regression(sess, "ariel", n_paths=200, seed=42)
        assert r2.flag_fired is not None
        # r2.prev_run_date should be the (mutated) row's snapshot, which
        # the helper kept as today.
        assert r2.prev_run_date is not None
        # delta_pp is curr - prior * 100 ≈ -10pp
        assert r2.flag_fired.delta_pp < -DEFAULT_MC_DELTA_PP_THRESHOLD
    finally:
        sess.close()
