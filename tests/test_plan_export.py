"""Tests for the markdown plan-export endpoint.

Covers:
  * Quick Reference section included (extracted from baseline raw_markdown
    or via the draft's horizon_long_md fallback).
  * Wealth dashboard numbers (net worth / burn / NVDA concentration) make
    it into the output.
  * Action items surfaced.
  * Pending FM objections rendered when a draft has them.
  * Falls back gracefully when no pending draft exists (uses current plan).
  * Self-review counts rendered when a FleetSelfReviewReport is present.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from argosy.state.models import (
    AgentReport,
    FleetSelfReviewReport,
    PlanVersion,
    PortfolioSnapshotRow,
    User,
    UserContext,
)


# ---------------------------------------------------------------------------
# Helpers — seed fixtures for the export tests.
# ---------------------------------------------------------------------------


def _seed_user(client_with_db, user_id: str = "ariel") -> None:
    sess = client_with_db.app.state.session_factory()
    try:
        if sess.get(User, user_id) is None:
            sess.add(User(id=user_id, plan="free"))
            sess.commit()
    finally:
        sess.close()


def _seed_snapshot(
    client_with_db,
    *,
    user_id: str = "ariel",
    total_usd_k: float = 3800.0,
    nvda_value_k: float = 2299.0,
    fx_usd_nis: float = 3.10,
) -> None:
    """Insert a portfolio_snapshots row + a UserContext identity_yaml so
    compute_wealth_dashboard returns populated numbers."""
    _seed_user(client_with_db, user_id)
    sess = client_with_db.app.state.session_factory()
    try:
        snap = PortfolioSnapshotRow(
            user_id=user_id,
            snapshot_date=date(2026, 5, 26),
            imported_at=datetime.now(timezone.utc),
            fx_usd_nis=fx_usd_nis,
            fx_usd_eur=0.92,
            positions_json=json.dumps([
                {
                    "symbol": "NVDA",
                    "asset_type": "Individual stocks",
                    "usd_value_k": nvda_value_k,
                    "location": "Schwab US",
                    "currency": "USD",
                    "current_price": 175.0,
                    "shares": 13140,
                },
                {
                    "symbol": "VOO",
                    "asset_type": "Core equity",
                    "usd_value_k": total_usd_k - nvda_value_k - 200.0,
                    "location": "Schwab US",
                    "currency": "USD",
                },
                {
                    "symbol": "SGOV",
                    "asset_type": "Cash",
                    "usd_value_k": 200.0,
                    "location": "Schwab US",
                    "currency": "USD",
                },
            ]),
            totals_json=json.dumps({
                "total_usd_value_k": total_usd_k,
            }),
            allocations_json=json.dumps([]),
        )
        sess.add(snap)

        # Identity context with a budget feed.
        ctx = UserContext(
            user_id=user_id,
            identity_yaml=(
                "user_date_of_birth: '1981-04-15'\n"
                "fx_rate:\n"
                "  usd_nis: 3.10\n"
            ),
            goals_yaml="",
            constraints_yaml="",
        )
        sess.add(ctx)

        # household_budget agent_report — feeds monthly burn/income/surplus.
        budget = AgentReport(
            user_id=user_id,
            agent_role="household_budget",
            decision_id="standalone",
            model="claude-opus-4-7",
            confidence=None,
            response_text=json.dumps({
                "monthly_burn_nis": 23100.0,
                "monthly_income_nis": 54800.0,
            }),
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            prompt_hash="",
        )
        sess.add(budget)
        sess.commit()
    finally:
        sess.close()


_BASELINE_MD = """# Plan v1

## Quick Reference

- Target NVDA share count: 8,000
- SWR: 3.5%
- USD/NIS: 3.10
- Cash floor: 12 months
- Concentration target: 45%

## Long horizon

Various things...

## Medium horizon

More things...
"""


def _seed_baseline(client_with_db, *, user_id: str = "ariel") -> int:
    _seed_user(client_with_db, user_id)
    sess = client_with_db.app.state.session_factory()
    try:
        pv = PlanVersion(
            user_id=user_id,
            role="baseline",
            version_label="baseline-test",
            raw_markdown=_BASELINE_MD,
        )
        sess.add(pv)
        sess.commit()
        sess.refresh(pv)
        return pv.id
    finally:
        sess.close()


def _seed_current(client_with_db, *, user_id: str = "ariel") -> int:
    """Seed an accepted plan with horizon markdown."""
    _seed_user(client_with_db, user_id)
    sess = client_with_db.app.state.session_factory()
    try:
        pv = PlanVersion(
            user_id=user_id,
            role="current",
            version_label="plan-vNow",
            raw_markdown=_BASELINE_MD,
            horizon_long_md=(
                "### Long horizon\n"
                "- Reduce NVDA to 45% over 18 months.\n"
                "- Maintain $2M cash floor.\n"
            ),
            horizon_medium_md=(
                "### Medium horizon\n"
                "- Quarterly tranches; reassess after each.\n"
            ),
            horizon_short_md=(
                "### Short horizon\n"
                "- 2026-06-15: Engage estate attorney.\n"
            ),
            horizon_short_json=json.dumps({
                "horizon": "short",
                "freshness_expected": "monthly",
                "status": "minor_revision",
                "posture": "test",
                "targets": [],
                "themes": [],
                "actions": [
                    {
                        "label": "Engage estate attorney",
                        "horizon_kind": "dated",
                        "trigger_or_date": (date.today() + timedelta(days=5)).isoformat(),
                        "detail": "Re: US-situs exposure",
                        "rationale": "estate liability",
                        "cited_sources": [],
                    },
                ],
                "deltas_from_prior": [],
                "rationale": "",
                "cited_sources": [],
            }),
            horizon_medium_json=json.dumps({
                "horizon": "medium",
                "freshness_expected": "quarterly",
                "status": "minor_revision",
                "posture": "test",
                "targets": [{
                    "label": "NVDA pct of portfolio",
                    "unit": "pct_of_portfolio",
                    "value": 45.0,
                }],
                "themes": [],
                "actions": [],
                "deltas_from_prior": [],
                "rationale": "",
                "cited_sources": [],
            }),
            accepted_at=datetime.now(timezone.utc),
        )
        sess.add(pv)
        sess.commit()
        sess.refresh(pv)
        return pv.id
    finally:
        sess.close()


def _seed_draft_with_fm_objections(
    client_with_db,
    *,
    user_id: str = "ariel",
    decision_run_id: int = 999,
    reasons: list[str] | None = None,
) -> int:
    """Seed a role='draft' plan_version with a backing fund_manager
    agent_report so the export emits the FM objections section."""
    _seed_user(client_with_db, user_id)
    sess = client_with_db.app.state.session_factory()
    try:
        pv = PlanVersion(
            user_id=user_id,
            role="draft",
            version_label="draft-test",
            raw_markdown="",
            decision_run_id=decision_run_id,
            horizon_long_md="### Long\nFoo.\n",
            horizon_medium_md="### Medium\nBar.\n",
            horizon_short_md="### Short\nBaz.\n",
            horizon_short_json=json.dumps({
                "horizon": "short",
                "freshness_expected": "monthly",
                "status": "minor_revision",
                "posture": "test",
                "targets": [],
                "themes": [],
                "actions": [],
                "deltas_from_prior": [],
                "rationale": "",
                "cited_sources": [],
            }),
            horizon_medium_json=json.dumps({
                "horizon": "medium",
                "freshness_expected": "quarterly",
                "status": "minor_revision",
                "posture": "test",
                "targets": [],
                "themes": [],
                "actions": [],
                "deltas_from_prior": [],
                "rationale": "",
                "cited_sources": [],
            }),
        )
        sess.add(pv)
        sess.commit()
        sess.refresh(pv)

        reasons = reasons or [
            "[BLOCKER — Section 102 risk] Statutory deadline gating",
            "Concentration unquantified — clarify NVDA reduction pace",
        ]
        fm = AgentReport(
            user_id=user_id,
            agent_role="fund_manager",
            decision_id=f"plan-synth-{decision_run_id}",
            model="claude-opus-4-7",
            confidence=None,
            response_text=json.dumps({
                "approved": False,
                "reasons": reasons,
                "cited_sources": [],
            }),
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            prompt_hash="",
        )
        sess.add(fm)
        sess.commit()
        return pv.id
    finally:
        sess.close()


def _seed_self_review(
    client_with_db,
    *,
    user_id: str = "ariel",
    red: int = 0,
    amber: int = 3,
    yellow: int = 5,
) -> None:
    sess = client_with_db.app.state.session_factory()
    try:
        sess.add(FleetSelfReviewReport(
            user_id=user_id,
            scope_kind="post_synthesis",
            content_md="dummy",
            findings_json="[]",
            severity_summary_json=json.dumps({
                "RED": red, "AMBER": amber, "YELLOW": yellow,
            }),
        ))
        sess.commit()
    finally:
        sess.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_export_includes_quick_reference(client_with_db):
    """The Quick Reference section in baseline raw_markdown is surfaced
    under the export's ``### Quick Reference`` heading."""
    _seed_baseline(client_with_db)
    r = client_with_db.get("/api/plan/export?user_id=ariel")
    assert r.status_code == 200, r.text
    body = r.text
    assert "### Quick Reference" in body
    # Numbers from _BASELINE_MD's Quick Reference block.
    assert "Target NVDA share count" in body
    assert "SWR: 3.5%" in body
    # Content-Disposition triggers a browser download.
    cd = r.headers.get("content-disposition") or ""
    assert "attachment" in cd
    assert "argosy-plan-" in cd
    assert cd.endswith('.md"')


def test_export_includes_wealth_dashboard_numbers(client_with_db):
    """Net worth (with $-suffix variant), monthly burn/income/surplus, NVDA
    concentration and retirement scenarios are present in the output."""
    _seed_snapshot(client_with_db)
    _seed_current(client_with_db)
    r = client_with_db.get("/api/plan/export?user_id=ariel")
    assert r.status_code == 200, r.text
    body = r.text
    # Net worth — total_usd_k is 3800 -> 3.80M USD, * 3.10 = 11.78M NIS.
    # The dashboard figure is the TOTAL basis (incl. real estate); its label
    # must say so explicitly so it never reads as the same concept as the body's
    # liquid/investable net worth (the 11.95M-vs-14.15M coherence defect).
    assert "Total net worth (incl. real estate):" in body
    assert "M NIS" in body
    assert "$3" in body  # $3.80M USD
    # Monthly numbers from the household_budget agent_report.
    assert "23.1K NIS" in body  # monthly burn
    assert "54.8K NIS" in body  # monthly income
    assert "31.7K NIS" in body or "31.8K NIS" in body  # surplus
    # NVDA concentration (single ticker in our seed).
    assert "NVDA concentration:" in body
    # Retirement scenarios table.
    assert "| Scenario |" in body
    assert "| Bear |" in body
    assert "| Conservative |" in body
    assert "| Typical |" in body
    # Coherence guard (run-102 reader BLOCKER): the per-scenario age column
    # must be labelled as a per-scenario FI age, NOT a bare "Target age" that
    # reads as a fourth headline retirement age. The caption must say it is
    # distinct from the Monte-Carlo earliest-safe headline age.
    assert "FI age (this scenario)" in body
    assert "Target age |" not in body
    assert "not the headline retirement age" in body


def test_export_includes_action_items(client_with_db):
    """The dated action seeded in the short-horizon JSON appears as a bullet
    under ``## Action Items``."""
    _seed_current(client_with_db)
    r = client_with_db.get("/api/plan/export?user_id=ariel")
    assert r.status_code == 200, r.text
    body = r.text
    assert "## Action Items" in body
    # The seeded action.
    assert "Engage estate attorney" in body
    # And the dated bullet form.
    expected_date = (date.today() + timedelta(days=5)).isoformat()
    assert expected_date in body


def test_export_includes_fm_objections_when_pending_draft(client_with_db):
    """A draft with a fund_manager agent_report should render the
    ``## Pending FM objections`` section with the parsed reasons."""
    _seed_draft_with_fm_objections(client_with_db)
    r = client_with_db.get("/api/plan/export?user_id=ariel")
    assert r.status_code == 200, r.text
    body = r.text
    assert "## Pending FM objections" in body
    # Severity classification picks up [BLOCKER — ...]
    assert "[BLOCKER]" in body
    assert "Section 102" in body
    # Second reason (no severity prefix) classified by keywords.
    assert "Concentration unquantified" in body


def test_export_falls_back_when_no_pending_draft(client_with_db):
    """With only a current accepted plan (no draft), the export still
    builds — status reads 'Accepted (current)' and FM objections section
    is absent."""
    _seed_current(client_with_db)
    r = client_with_db.get("/api/plan/export?user_id=ariel")
    assert r.status_code == 200, r.text
    body = r.text
    assert "Status: Accepted (current)" in body
    assert "## Pending FM objections" not in body
    # Long/medium/short horizon markdown still surfaces.
    assert "## Long-horizon plan" in body
    assert "Reduce NVDA to 45% over 18 months." in body


def test_export_includes_self_review_counts(client_with_db):
    """When a FleetSelfReviewReport exists, the Notes section surfaces its
    RED/AMBER/YELLOW counts."""
    _seed_current(client_with_db)
    _seed_self_review(client_with_db, red=1, amber=4, yellow=7)
    r = client_with_db.get("/api/plan/export?user_id=ariel")
    assert r.status_code == 200, r.text
    body = r.text
    assert "Self-review: 1 RED, 4 AMBER, 7 YELLOW" in body
