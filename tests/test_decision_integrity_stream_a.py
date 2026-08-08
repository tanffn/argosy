"""Stream A integrity gates — fix iteration 1 (adversarial re-review).

Covers blockers 1–8. Tests are designed so reverting the corresponding
fix makes them fail (stated per case).
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from argosy.agents.base import AgentReport, ConfidenceBand
from argosy.agents.remediation import RemediationRequest
from argosy.agents.trader import ExpectedImpact, TraderProposal
from argosy.decisions.flow import (
    ApprovedProposal,
    BlockedProposal,
    DecisionFlow,
    FlowConfig,
)
from argosy.decisions.tiers import Tier
from argosy.services.decision_integrity.as_of import (
    attach_provenance_sidecar,
    format_field_for_prompt,
    inferred_period_end_from_release,
    period_end_for_quarter,
    stamp_fundamentals_payload,
)
from argosy.services.decision_integrity.confidence_cap import (
    observe_confidence_delta,
)
from argosy.services.decision_integrity.gates import (
    evaluate_green_light_integrity,
)
from argosy.services.decision_integrity.overrides import (
    debate_action_contradicts_winning_side,
)
from argosy.services.decision_integrity.remediation_store import (
    auto_resolve_on_fresh_pass,
    has_open_remediation,
    override_remediation,
    persist_remediation_requests,
    resolve_remediation,
)
from argosy.services.decision_integrity.vintage_gate import evaluate_vintage_gate


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def integrity_session(alembic_engine_at_head):
    eng = alembic_engine_at_head
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT OR IGNORE INTO users (id, plan, created_at) "
                "VALUES ('ariel', 'free', CURRENT_TIMESTAMP)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO decision_runs "
                "(user_id, ticker, tier, started_at, status) "
                "VALUES ('ariel', 'TRLV', 'T2', CURRENT_TIMESTAMP, 'running')"
            )
        )
    SessionLocal = sessionmaker(bind=eng, expire_on_commit=False)
    sess = SessionLocal()
    run_id = sess.execute(
        text("SELECT id FROM decision_runs ORDER BY id DESC LIMIT 1")
    ).scalar_one()
    yield sess, int(run_id)
    sess.close()


def _trlv_stale_fields() -> dict[str, Any]:
    """Q1 figures still served after Q2 has been reported (TRLV shape)."""
    return {
        "revenue_growth_yoy": -1.39,
        "net_income_ttm": -76_000_000,
        "current_price": 5.2,
        "financials_as_of": "2026-03-31",  # Q1 period end
        "most_recent_reported_period": "2026-06-30",  # Q2 reported
        "most_recent_earnings_date": "2026-08-06",  # release day (informational)
    }


def _trlv_fresh_fields() -> dict[str, Any]:
    """Genuine post-Q2 payload — period matches latest reported period."""
    return {
        "revenue_growth_yoy": -0.10,
        "net_income_ttm": -406_000_000,
        "current_price": 5.2,
        "financials_as_of": "2026-06-30",
        "most_recent_reported_period": "2026-06-30",
        "most_recent_earnings_date": "2026-08-06",
    }


# ----------------------------------------------------------------------
# Blocker 8 — scalar contract preserved (sidecar, not wrappers)
# ----------------------------------------------------------------------


def test_sidecar_preserves_scalar_values() -> None:
    """FAILS if stamp wraps values as {value, as_of} again."""
    raw = {
        "TRLV": {
            "revenue_growth_yoy": -1.39,
            "eps_ttm": 1.5,
            "current_price": 5.2,
            "financials_as_of": "2026-03-31",
            "source_url": "yfinance:TRLV",
        }
    }
    out = attach_provenance_sidecar(raw)
    assert out["TRLV"]["revenue_growth_yoy"] == -1.39
    assert isinstance(out["TRLV"]["revenue_growth_yoy"], float)
    assert out["TRLV"]["eps_ttm"] == 1.5
    assert out["TRLV"]["financials_as_of"] == "2026-03-31"
    # Deprecated alias must also preserve scalars.
    stamped = stamp_fundamentals_payload(raw)
    assert stamped["TRLV"]["revenue_growth_yoy"] == -1.39


def test_prompt_label_uses_sidecar_as_of() -> None:
    fields = {
        "revenue_growth_yoy": -1.39,
        "financials_as_of": "2026-03-31",
    }
    label = format_field_for_prompt(fields, "revenue_growth_yoy")
    assert "as of 2026-03-31" in label
    assert "-1.39" in label


# ----------------------------------------------------------------------
# Blocker 1 — unknown provenance BLOCKS (never defaults to today / pass)
# ----------------------------------------------------------------------


def test_missing_financials_as_of_blocks() -> None:
    """FAILS if missing as_of is treated as fresh/pass."""
    fields = {
        "revenue_growth_yoy": -1.39,
        "most_recent_reported_period": "2026-06-30",
        # financials_as_of intentionally absent
    }
    result = evaluate_vintage_gate("TRLV", fields)
    assert result.block is True
    assert result.blocked_by == "provenance_unknown"


def test_missing_reported_period_blocks() -> None:
    """FAILS if calendar miss / absent reported period passes."""
    fields = {
        "revenue_growth_yoy": -1.39,
        "financials_as_of": "2026-03-31",
        # most_recent_reported_period intentionally absent
    }
    result = evaluate_vintage_gate("TRLV", fields)
    assert result.block is True
    assert result.blocked_by == "provenance_unknown"


def test_stamp_does_not_invent_today_as_financials_as_of() -> None:
    """FAILS if attach/stamp defaults missing financials_as_of to today."""
    raw = {"X": {"eps_ttm": 1.0}}
    out = attach_provenance_sidecar(raw)
    assert "financials_as_of" not in out["X"] or out["X"]["financials_as_of"] is None
    assert out["X"].get("provenance_complete") is not True


# ----------------------------------------------------------------------
# Blocker 3 — period-vs-period (fresh Q2 PASSES; stale Q1 BLOCKS)
# ----------------------------------------------------------------------


def test_fresh_quarterly_data_passes_after_release() -> None:
    """FAILS if period-end is compared to release day (inverted semantics)."""
    # Q2 period end Jun 30, release Aug 6 — must PASS.
    result = evaluate_vintage_gate("TRLV", _trlv_fresh_fields())
    assert result.ok is True, result.reason


def test_stale_prior_quarter_blocks() -> None:
    """FAILS if Q1 data after Q2 reported is allowed through."""
    result = evaluate_vintage_gate("TRLV", _trlv_stale_fields())
    assert result.block is True
    assert result.blocked_by == "vintage_stale"


def test_period_helpers_map_release_to_reported_period() -> None:
    assert period_end_for_quarter(2026, 2) == date(2026, 6, 30)
    assert inferred_period_end_from_release(date(2026, 8, 6)) == date(2026, 6, 30)


# ----------------------------------------------------------------------
# Blocker 2 — infrastructure failure fail-CLOSED
# ----------------------------------------------------------------------


def test_db_failure_blocks_green_light(integrity_session) -> None:
    """FAILS if a broken session/query resolves to approval."""
    sess, run_id = integrity_session

    class _Boom:
        def execute(self, *a, **k):
            raise RuntimeError("simulated sqlite lock / missing table")

        def scalars(self, *a, **k):
            raise RuntimeError("simulated sqlite lock / missing table")

    # Evaluate with a session whose queries explode.
    boom = MagicMock()
    boom.execute.side_effect = RuntimeError("simulated migration missing")
    # list_open_remediations uses session.scalars(select...) — make scalars fail.
    boom.scalars.side_effect = RuntimeError("simulated migration missing")

    gate = evaluate_green_light_integrity(
        boom,
        user_id="ariel",
        ticker="TRLV",
        decision_run_id=run_id,
        fundamentals_fields=_trlv_fresh_fields(),
        analyst_reports=[],
        skip_db=False,
    )
    assert gate.block is True
    assert gate.blocked_by == "integrity_gate_error"


def test_none_session_without_skip_db_blocks() -> None:
    """FAILS if session=None is treated as safe pass."""
    gate = evaluate_green_light_integrity(
        None,
        user_id="ariel",
        ticker="TRLV",
        fundamentals_fields=_trlv_fresh_fields(),
        skip_db=False,
    )
    assert gate.block is True
    assert gate.blocked_by == "integrity_gate_error"


# ----------------------------------------------------------------------
# Blocker 5 — choke point covers pre-built analyst reports
# ----------------------------------------------------------------------


class _FundOut(BaseModel):
    cited_sources: list[str] = ["fundamentals/TRLV"]
    remediation_requests: list[RemediationRequest] = Field(default_factory=list)
    summary: str = ""
    confidence: ConfidenceBand = ConfidenceBand.LOW


def _report_with_remediation() -> AgentReport:
    req = RemediationRequest(
        kind="data_integrity",
        target_role="fundamentals",
        reason="market_cap/price divergence >10%",
        ticker="TRLV",
    )
    return AgentReport(
        agent_role="fundamentals",
        user_id="ariel",
        model="test",
        response_text="{}",
        tokens_in=1,
        tokens_out=1,
        cost_usd=0.0,
        prompt_hash="h",
        confidence=ConfidenceBand.LOW,
        output=_FundOut(remediation_requests=[req]),
    )


@pytest.mark.asyncio
async def test_prebuilt_analyst_reports_with_remediation_block_flow() -> None:
    """FAILS if DecisionFlow ignores remediation_requests on input reports.

    Reconstructs the entry path that supplies pre-built reports (CLI /
    analyst_report_ids) and never calls run_per_ticker_analysts.
    """

    class _Trader:
        async def run(self, **kwargs):
            return AgentReport(
                agent_role="trader",
                user_id="ariel",
                model="t",
                response_text="{}",
                tokens_in=1,
                tokens_out=1,
                cost_usd=0.0,
                prompt_hash="h",
                confidence=ConfidenceBand.LOW,
                output=TraderProposal(
                    ticker="TRLV",
                    action="buy",
                    size_shares_or_currency=100,
                    size_units="shares",
                    instrument="stock",
                    rationale_summary="buy despite data break",
                    expected_impact=ExpectedImpact(),
                    confidence=ConfidenceBand.LOW,
                    cited_sources=["fundamentals/TRLV"],
                ),
            )

    flow = DecisionFlow(
        user_id="ariel",
        config=FlowConfig(skip_persistence=True),
        trader_factory=lambda u, t: _Trader(),
    )
    outcome = await flow.run(
        ticker="TRLV",
        tier=Tier.T0,
        analyst_reports=[_report_with_remediation()],
        # Pre-built reports only — no per-ticker gather (blocker 5 path).
        funnel_meta={"fundamentals_fields": _trlv_fresh_fields()},
    )
    assert isinstance(outcome, BlockedProposal)
    assert outcome.blocked_by == "open_remediation"


def test_in_memory_remediation_blocks_without_db() -> None:
    """Pure choke-point check used by every DecisionFlow entry path."""
    gate = evaluate_green_light_integrity(
        None,
        user_id="ariel",
        ticker="TRLV",
        fundamentals_fields=_trlv_fresh_fields(),
        analyst_reports=[_report_with_remediation()],
        skip_db=True,
    )
    assert gate.block is True
    assert gate.blocked_by == "open_remediation"


# ----------------------------------------------------------------------
# Blocker 4 — auto-resolve + operator override
# ----------------------------------------------------------------------


def test_auto_resolve_on_fresh_pass_clears_opens(integrity_session) -> None:
    """FAILS if open rows permanently deadlock a ticker."""
    sess, run_id = integrity_session
    persist_remediation_requests(
        sess,
        user_id="ariel",
        requests=[
            RemediationRequest(
                kind="vintage_stale",
                target_role="fundamentals",
                reason="stale",
                ticker="TRLV",
            )
        ],
        decision_run_id=run_id,
    )
    sess.commit()
    assert has_open_remediation(sess, user_id="ariel", ticker="TRLV")
    n = auto_resolve_on_fresh_pass(sess, user_id="ariel", ticker="TRLV")
    sess.commit()
    assert n >= 1
    assert not has_open_remediation(sess, user_id="ariel", ticker="TRLV")


def test_operator_override_requires_reason(integrity_session) -> None:
    sess, run_id = integrity_session
    rows = persist_remediation_requests(
        sess,
        user_id="ariel",
        requests=[
            RemediationRequest(
                kind="data_integrity",
                target_role="fundamentals",
                reason="break",
                ticker="TRLV",
            )
        ],
        decision_run_id=run_id,
    )
    sess.commit()
    with pytest.raises(ValueError):
        override_remediation(
            sess, rows[0].id, user_id="ariel", override_reason="  ",
        )
    override_remediation(
        sess,
        rows[0].id,
        user_id="ariel",
        override_reason="Ariel accepts residual risk",
    )
    sess.commit()
    assert not has_open_remediation(sess, user_id="ariel", ticker="TRLV")


# ----------------------------------------------------------------------
# Blocker 6 — confidence is observed, never mutated
# ----------------------------------------------------------------------


def test_confidence_delta_observed_not_mutated() -> None:
    """FAILS if observe returns a capped/replaced band."""
    emitted, rose, floor = observe_confidence_delta(
        ConfidenceBand.MEDIUM,
        [ConfidenceBand.LOW, ConfidenceBand.LOW],
    )
    assert rose is True
    assert emitted == ConfidenceBand.MEDIUM  # NOT clipped to LOW
    assert floor == ConfidenceBand.LOW


# ----------------------------------------------------------------------
# Blocker 7 — TRLV end-to-end shape through the real gate + flow
# ----------------------------------------------------------------------


def test_trlv_regression_gate_blocks_stale_and_passes_fresh(
    integrity_session,
) -> None:
    sess, run_id = integrity_session

    stale = evaluate_green_light_integrity(
        sess,
        user_id="ariel",
        ticker="TRLV",
        decision_run_id=run_id,
        fundamentals_fields=_trlv_stale_fields(),
        analyst_reports=[],
    )
    assert stale.block is True
    assert stale.blocked_by == "vintage_stale"

    # Open remediation also blocks even when vintage is fresh.
    persist_remediation_requests(
        sess,
        user_id="ariel",
        requests=[
            RemediationRequest(
                kind="data_integrity",
                target_role="fundamentals",
                reason="market_cap/price divergence >10%",
                ticker="TRLV",
            )
        ],
        decision_run_id=run_id,
    )
    sess.commit()
    rem = evaluate_green_light_integrity(
        sess,
        user_id="ariel",
        ticker="TRLV",
        decision_run_id=run_id,
        fundamentals_fields=_trlv_fresh_fields(),
        analyst_reports=[],
    )
    assert rem.block is True
    assert rem.blocked_by == "open_remediation"

    # Vintage pass must NOT launder data_integrity (iter-2 item 3).
    n = auto_resolve_on_fresh_pass(sess, user_id="ariel", ticker="TRLV")
    sess.commit()
    assert n == 0
    still = evaluate_green_light_integrity(
        sess,
        user_id="ariel",
        ticker="TRLV",
        decision_run_id=run_id,
        fundamentals_fields=_trlv_fresh_fields(),
        analyst_reports=[],
    )
    assert still.block is True
    assert still.blocked_by == "open_remediation"

    # Explicit resolve of the integrity break → pass.
    open_rows = [
        r for r in sess.execute(
            text(
                "SELECT id FROM remediation_requests "
                "WHERE status='open' AND ticker='TRLV'"
            )
        ).all()
    ]
    for (rid,) in open_rows:
        resolve_remediation(sess, int(rid), user_id="ariel")
    sess.commit()
    ok = evaluate_green_light_integrity(
        sess,
        user_id="ariel",
        ticker="TRLV",
        decision_run_id=run_id,
        fundamentals_fields=_trlv_fresh_fields(),
        analyst_reports=[],
    )
    assert ok.block is False


@pytest.mark.asyncio
async def test_decision_flow_blocks_on_stale_vintage_via_funnel_meta() -> None:
    """Drives DecisionFlow.run with funnel_meta fundamentals — must block."""

    class _Trader:
        async def run(self, **kwargs):
            return AgentReport(
                agent_role="trader",
                user_id="ariel",
                model="t",
                response_text="{}",
                tokens_in=1,
                tokens_out=1,
                cost_usd=0.0,
                prompt_hash="h",
                confidence=ConfidenceBand.LOW,
                output=TraderProposal(
                    ticker="TRLV",
                    action="buy",
                    size_shares_or_currency=10,
                    size_units="shares",
                    instrument="stock",
                    rationale_summary="buy",
                    expected_impact=ExpectedImpact(),
                    confidence=ConfidenceBand.LOW,
                    cited_sources=["x"],
                ),
            )

    # T0 skips debate/FM — integrity gate still runs before proposal.
    flow = DecisionFlow(
        user_id="ariel",
        config=FlowConfig(skip_persistence=True),
        trader_factory=lambda u, t: _Trader(),
    )
    outcome = await flow.run(
        ticker="TRLV",
        tier=Tier.T0,
        analyst_reports=[],  # no fundamentals report → vintage from funnel_meta
        funnel_meta={"fundamentals_fields": _trlv_stale_fields()},
    )
    assert isinstance(outcome, BlockedProposal)
    assert outcome.blocked_by == "vintage_stale"


@pytest.mark.asyncio
async def test_decision_flow_passes_fresh_vintage_t0() -> None:
    class _Trader:
        async def run(self, **kwargs):
            return AgentReport(
                agent_role="trader",
                user_id="ariel",
                model="t",
                response_text="{}",
                tokens_in=1,
                tokens_out=1,
                cost_usd=0.0,
                prompt_hash="h",
                confidence=ConfidenceBand.MEDIUM,
                output=TraderProposal(
                    ticker="TRLV",
                    action="buy",
                    size_shares_or_currency=10,
                    size_units="shares",
                    instrument="stock",
                    rationale_summary="buy",
                    expected_impact=ExpectedImpact(),
                    confidence=ConfidenceBand.MEDIUM,
                    cited_sources=["x"],
                ),
            )

    flow = DecisionFlow(
        user_id="ariel",
        config=FlowConfig(skip_persistence=True),
        trader_factory=lambda u, t: _Trader(),
    )
    outcome = await flow.run(
        ticker="TRLV",
        tier=Tier.T0,
        analyst_reports=[],
        funnel_meta={"fundamentals_fields": _trlv_fresh_fields()},
    )
    assert isinstance(outcome, ApprovedProposal)


def test_migration_0095_tables_exist(alembic_engine_at_head) -> None:
    eng = alembic_engine_at_head
    with eng.connect() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
    assert "remediation_requests" in tables
    assert "decision_overrides" in tables


def test_debate_override_detection_iova_shape() -> None:
    assert debate_action_contradicts_winning_side(
        winning_side="bear", trade_action="BUY"
    )


# ======================================================================
# Fix iteration 2
# ======================================================================


def test_resolve_scoped_by_user_id(integrity_session) -> None:
    """FAILS if resolve_remediation ignores user_id (cross-tenant clear)."""
    sess, run_id = integrity_session
    sess.execute(
        text(
            "INSERT OR IGNORE INTO users (id, plan, created_at) "
            "VALUES ('other', 'free', CURRENT_TIMESTAMP)"
        )
    )
    sess.commit()
    rows = persist_remediation_requests(
        sess,
        user_id="ariel",
        requests=[
            RemediationRequest(
                kind="vintage_stale",
                target_role="fundamentals",
                reason="stale",
                ticker="TRLV",
            )
        ],
        decision_run_id=run_id,
    )
    sess.commit()
    rid = rows[0].id
    assert resolve_remediation(sess, rid, user_id="other") is None
    assert has_open_remediation(sess, user_id="ariel", ticker="TRLV")
    assert resolve_remediation(sess, rid, user_id="ariel") is not None
    sess.commit()
    assert not has_open_remediation(sess, user_id="ariel", ticker="TRLV")


def test_absent_fundamentals_fields_blocks_by_default() -> None:
    """FAILS if absent fields pass when require flag is default-on."""
    gate = evaluate_green_light_integrity(
        None,
        user_id="ariel",
        ticker="TRLV",
        fundamentals_fields=None,
        skip_db=True,
    )
    assert gate.block is True
    assert gate.blocked_by == "provenance_unknown"


@pytest.mark.asyncio
async def test_decision_flow_blocks_absent_provenance_report_ids_path() -> None:
    """API report-IDs / CLI-without-gather / empty funnel_meta → block.

    FAILS if DecisionFlow green-lights with no fundamentals_fields.
    """

    class _Trader:
        async def run(self, **kwargs):
            return AgentReport(
                agent_role="trader",
                user_id="ariel",
                model="t",
                response_text="{}",
                tokens_in=1,
                tokens_out=1,
                cost_usd=0.0,
                prompt_hash="h",
                confidence=ConfidenceBand.LOW,
                output=TraderProposal(
                    ticker="TRLV",
                    action="buy",
                    size_shares_or_currency=10,
                    size_units="shares",
                    instrument="stock",
                    rationale_summary="buy",
                    expected_impact=ExpectedImpact(),
                    confidence=ConfidenceBand.LOW,
                    cited_sources=["x"],
                ),
            )

    flow = DecisionFlow(
        user_id="ariel",
        config=FlowConfig(skip_persistence=True),
        trader_factory=lambda u, t: _Trader(),
    )
    outcome = await flow.run(
        ticker="TRLV",
        tier=Tier.T0,
        analyst_reports=[],
        # No funnel_meta fundamentals — the report-ID / T0 / CLI-miss path.
    )
    assert isinstance(outcome, BlockedProposal)
    assert outcome.blocked_by == "provenance_unknown"


def test_empty_fields_dict_blocks_as_provenance_unknown() -> None:
    """Empty gather still blocks — never a silent pass."""
    gate = evaluate_green_light_integrity(
        None,
        user_id="ariel",
        ticker="TRLV",
        fundamentals_fields={},
        skip_db=True,
    )
    assert gate.block is True
    assert gate.blocked_by == "provenance_unknown"


def test_date_only_earnings_event_is_unknown_period() -> None:
    """FAILS if release date is synthesized into a fiscal period end."""
    from argosy.services.decision_integrity.as_of import (
        reported_period_from_earnings_event,
    )

    assert (
        reported_period_from_earnings_event({"date": "2026-08-06"}) is None
    )
    assert (
        reported_period_from_earnings_event(
            {"date": "2026-08-06", "quarter": 2, "year": 2026}
        )
        == date(2026, 6, 30)
    )


def test_gather_fundamentals_uses_sync_earnings_enrich_not_asyncio_run() -> None:
    """FAILS if earnings enrich reintroduces asyncio.run (iter-3/4)."""
    from pathlib import Path

    src = Path(
        "argosy/orchestrator/flows/plan_synthesis/inputs.py"
    ).read_text(encoding="utf-8")
    assert "_enrich_reported_periods_sync" in src
    assert "fetch_earnings_calendar_sync" in src
    # No asyncio.run wrapping the calendar call.
    assert "asyncio.run(\n                    adapter.get_earnings_calendar" not in src
    assert "asyncio.run(adapter.get_earnings_calendar" not in src
    assert "asyncio.run(\n                    adapter.fetch_earnings_calendar" not in src


def test_sync_enrich_sets_sourced_reported_period(monkeypatch) -> None:
    """FAILS if enrich does not write most_recent_reported_period from Q+Y."""
    from argosy.orchestrator.flows.plan_synthesis import inputs as inp

    class _FakeAdapter:
        def fetch_earnings_calendar_sync(self, *, start, end, symbol=None):
            return [
                {"date": "2026-08-06", "quarter": 2, "year": 2026},
            ]

    monkeypatch.setattr(
        "argosy.adapters.data.finnhub_adapter.FinnhubAdapter",
        lambda: _FakeAdapter(),
    )
    payload = {
        "AAPL": {
            "revenue_growth_yoy": 0.05,
            "financials_as_of": "2026-06-30",
            "pe_ratio": 30.0,
        }
    }
    inp._enrich_reported_periods_sync(payload)
    assert payload["AAPL"]["most_recent_reported_period"] == "2026-06-30"
    assert payload["AAPL"]["most_recent_reported_period_sourced"] is True
    assert payload["AAPL"]["reported_period_enrichment"] == "ok"


def test_production_shaped_payload_passes_gate_and_flow() -> None:
    """Liveness canary — FAILS if a realistic fresh payload cannot green-light.

    Shape matches Finnhub metrics + yfinance mostRecentQuarter + sync
    calendar enrich (quarter+year → period end).
    """
    fields = {
        "pe_ratio": 28.5,
        "eps_ttm": 6.4,
        "revenue_growth_yoy": 0.08,
        "current_price": 190.0,
        "financials_as_of": "2026-06-30",  # yfinance mostRecentQuarter / FH series
        "most_recent_reported_period": "2026-06-30",  # calendar Q2/2026
        "most_recent_reported_period_sourced": True,
        "reported_period_enrichment": "ok",
        "source_url": "yfinance:AAPL",
    }
    gate = evaluate_green_light_integrity(
        None,
        user_id="ariel",
        ticker="AAPL",
        fundamentals_fields=fields,
        analyst_reports=[],
        skip_db=True,
    )
    assert gate.block is False, gate.reason
    assert evaluate_vintage_gate("AAPL", fields).ok is True


@pytest.mark.asyncio
async def test_liveness_decision_flow_approves_production_shaped_payload() -> None:
    """End-to-end: realistic provenance reaches ApprovedProposal (T0)."""
    fields = {
        "pe_ratio": 28.5,
        "revenue_growth_yoy": 0.08,
        "current_price": 190.0,
        "financials_as_of": "2026-06-30",
        "most_recent_reported_period": "2026-06-30",
        "most_recent_reported_period_sourced": True,
    }

    class _Trader:
        async def run(self, **kwargs):
            return AgentReport(
                agent_role="trader",
                user_id="ariel",
                model="t",
                response_text="{}",
                tokens_in=1,
                tokens_out=1,
                cost_usd=0.0,
                prompt_hash="h",
                confidence=ConfidenceBand.MEDIUM,
                output=TraderProposal(
                    ticker="AAPL",
                    action="buy",
                    size_shares_or_currency=10,
                    size_units="shares",
                    instrument="stock",
                    rationale_summary="buy",
                    expected_impact=ExpectedImpact(),
                    confidence=ConfidenceBand.MEDIUM,
                    cited_sources=["x"],
                ),
            )

    flow = DecisionFlow(
        user_id="ariel",
        config=FlowConfig(skip_persistence=True),
        trader_factory=lambda u, t: _Trader(),
    )
    outcome = await flow.run(
        ticker="AAPL",
        tier=Tier.T0,
        analyst_reports=[],
        funnel_meta={"fundamentals_fields": fields},
    )
    assert isinstance(outcome, ApprovedProposal)


def test_realistic_ticker_set_provenance_pass_rate(monkeypatch) -> None:
    """Quantify: after sync enrich, what fraction of a realistic set passes.

    FAILS if pass-rate is 0% (the dead-system failure mode).
    """
    from argosy.orchestrator.flows.plan_synthesis import inputs as inp

    # Simulated production: yfinance set financials_as_of; calendar has Q+Y.
    calendar = {
        "AAPL": [{"date": "2026-08-01", "quarter": 2, "year": 2026}],
        "MSFT": [{"date": "2026-07-25", "quarter": 2, "year": 2026}],
        "NVDA": [{"date": "2026-08-20", "quarter": 2, "year": 2026}],
        "GOOGL": [{"date": "2026-07-24", "quarter": 2, "year": 2026}],
        "AMZN": [{"date": "2026-08-01", "quarter": 2, "year": 2026}],
        # Date-only only — must NOT invent a period → fail closed for this one.
        "TRLV": [{"date": "2026-08-06"}],
    }

    class _FakeAdapter:
        def fetch_earnings_calendar_sync(self, *, start, end, symbol=None):
            return list(calendar.get((symbol or "").upper(), []))

    monkeypatch.setattr(
        "argosy.adapters.data.finnhub_adapter.FinnhubAdapter",
        lambda: _FakeAdapter(),
    )

    payload = {
        t: {
            "pe_ratio": 20.0,
            "revenue_growth_yoy": 0.1,
            "financials_as_of": "2026-06-30",
        }
        for t in calendar
    }
    # TRLV stale shape: data still on Q1 while we pretend calendar had Q2 —
    # but date-only means unsourced, so provenance_unknown not vintage_stale.
    payload["TRLV"]["financials_as_of"] = "2026-03-31"

    inp._enrich_reported_periods_sync(payload)
    results = [
        evaluate_vintage_gate(t, fields)
        for t, fields in payload.items()
    ]
    passed = sum(1 for r in results if r.ok)
    total = len(results)
    rate = passed / total
    # 5/6 liquid names with Q+Y calendar should pass; TRLV date-only blocks.
    assert passed >= 5, f"pass_rate={rate:.0%} ({passed}/{total})"
    assert rate >= 0.8
    assert any(
        (not r.ok) and r.ticker == "TRLV" for r in results
    ), "date-only TRLV must still block"


def test_stamp_without_reported_period_is_incomplete() -> None:
    """Without enrichment, missing reported period stays visible (fail-closed)."""
    out = attach_provenance_sidecar(
        {
            "TRLV": {
                "revenue_growth_yoy": -1.39,
                "financials_as_of": "2026-03-31",
            }
        }
    )
    assert out["TRLV"].get("provenance_complete") is not True
    assert not out["TRLV"].get("most_recent_reported_period")
    result = evaluate_vintage_gate("TRLV", out["TRLV"])
    assert result.block is True
    assert result.blocked_by == "provenance_unknown"


def test_vintage_pass_does_not_clear_data_integrity(integrity_session) -> None:
    """FAILS if auto_resolve_on_vintage_pass clears non-vintage kinds."""
    sess, run_id = integrity_session
    persist_remediation_requests(
        sess,
        user_id="ariel",
        requests=[
            RemediationRequest(
                kind="data_integrity",
                target_role="fundamentals",
                reason="market_cap vs price",
                ticker="TRLV",
            ),
            RemediationRequest(
                kind="facilitator_condition",
                target_role="researcher_facilitator",
                reason="unmet condition",
                ticker="TRLV",
            ),
            RemediationRequest(
                kind="vintage_stale",
                target_role="fundamentals",
                reason="stale",
                ticker="TRLV",
            ),
        ],
        decision_run_id=run_id,
    )
    sess.commit()
    gate = evaluate_green_light_integrity(
        sess,
        user_id="ariel",
        ticker="TRLV",
        decision_run_id=run_id,
        fundamentals_fields=_trlv_fresh_fields(),
        analyst_reports=[],
    )
    assert gate.block is True
    assert gate.blocked_by == "open_remediation"
    assert gate.auto_resolved_count >= 1  # vintage_stale cleared
    # Remaining opens are the integrity / facilitator rows.
    assert has_open_remediation(sess, user_id="ariel", ticker="TRLV")
    from argosy.services.decision_integrity.remediation_store import (
        list_open_remediations,
    )

    kinds = {r.kind for r in list_open_remediations(
        sess, user_id="ariel", ticker="TRLV",
    )}
    assert "vintage_stale" not in kinds
    assert "data_integrity" in kinds
    assert "facilitator_condition" in kinds


@pytest.mark.asyncio
async def test_flow_auto_resolves_vintage_stale_then_passes(engine: None) -> None:
    """Through DecisionFlow.run — vintage pass clears vintage_stale.

    FAILS if open vintage_stale permanently deadlocks because opens were
    checked before auto-resolve (original blocker 4 ordering bug).
    """
    from argosy.state import db as db_mod
    from argosy.state.models import User

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    async with db_mod.get_session() as session:
        def _seed(sync_session: Any) -> None:
            persist_remediation_requests(
                sync_session,
                user_id="ariel",
                requests=[
                    RemediationRequest(
                        kind="vintage_stale",
                        target_role="fundamentals",
                        reason="prior stale",
                        ticker="TRLV",
                    )
                ],
            )
            sync_session.commit()

        await session.run_sync(_seed)

    class _Trader:
        async def run(self, **kwargs):
            return AgentReport(
                agent_role="trader",
                user_id="ariel",
                model="t",
                response_text="{}",
                tokens_in=1,
                tokens_out=1,
                cost_usd=0.0,
                prompt_hash="h",
                confidence=ConfidenceBand.MEDIUM,
                output=TraderProposal(
                    ticker="TRLV",
                    action="buy",
                    size_shares_or_currency=10,
                    size_units="shares",
                    instrument="stock",
                    rationale_summary="buy",
                    expected_impact=ExpectedImpact(),
                    confidence=ConfidenceBand.MEDIUM,
                    cited_sources=["x"],
                ),
            )

    flow = DecisionFlow(
        user_id="ariel",
        trader_factory=lambda u, t: _Trader(),
    )
    outcome = await flow.run(
        ticker="TRLV",
        tier=Tier.T0,
        analyst_reports=[],
        funnel_meta={"fundamentals_fields": _trlv_fresh_fields()},
    )
    assert isinstance(outcome, ApprovedProposal), (
        getattr(outcome, "blocked_by", None),
        getattr(outcome, "reason", None),
    )

    async with db_mod.get_session() as session:
        def _check(sync_session: Any) -> bool:
            return has_open_remediation(
                sync_session, user_id="ariel", ticker="TRLV",
            )

        still_open = await session.run_sync(_check)
    assert still_open is False


def test_deep_decision_threads_fundamentals_into_funnel_meta() -> None:
    """FAILS if scheduled funnel omits fundamentals_fields (item 2 path)."""
    from pathlib import Path

    src = Path(
        "argosy/services/decision_funnel/deep_decision.py"
    ).read_text(encoding="utf-8")
    assert "fundamentals_fields" in src
    assert "fundamentals_payload" in src
    # Must assign from the gather result, not only mention the string.
    assert "result.fundamentals_payload" in src


def test_cli_decide_threads_fundamentals_into_funnel_meta() -> None:
    """FAILS if CLI green_light path omits fundamentals gather (item 2)."""
    from pathlib import Path

    src = Path("argosy/cli/decide.py").read_text(encoding="utf-8")
    assert "fundamentals_fields" in src
    assert "_refresh_fundamentals_payload" in src


def test_remediation_resolve_requires_totp(client_with_db) -> None:
    """FAILS if resolve/override accept unauthenticated clears."""
    from argosy.security import totp as totp_mod
    from argosy.state.models import RemediationRequestRecord, TOTPSecret, User

    SF = client_with_db.app.state.session_factory
    secret = totp_mod.generate_secret()
    other_secret = totp_mod.generate_secret()
    with SF() as s:
        s.add(User(id="ariel", plan="free"))
        s.add(User(id="other", plan="free"))
        s.add(TOTPSecret(user_id="ariel", secret_encrypted=secret))
        s.add(TOTPSecret(user_id="other", secret_encrypted=other_secret))
        row = RemediationRequestRecord(
            user_id="ariel",
            ticker="TRLV",
            kind="vintage_stale",
            target_role="fundamentals",
            reason="stale",
            status="open",
        )
        s.add(row)
        s.commit()
        rid = row.id

    # No TOTP header → 400
    r = client_with_db.post(
        f"/api/decisions/remediations/{rid}/resolve",
        json={"user_id": "ariel"},
    )
    assert r.status_code == 400, r.text
    assert "TOTP" in r.json()["detail"]

    # Cross-tenant: other user with valid TOTP cannot clear ariel's row.
    code = totp_mod.generate_code(other_secret)
    r2 = client_with_db.post(
        f"/api/decisions/remediations/{rid}/resolve",
        json={"user_id": "other"},
        headers={"X-TOTP-Code": code},
    )
    assert r2.status_code == 404, r2.text

    # Owner + valid TOTP → resolve.
    code_ok = totp_mod.generate_code(secret)
    r3 = client_with_db.post(
        f"/api/decisions/remediations/{rid}/resolve",
        json={"user_id": "ariel"},
        headers={"X-TOTP-Code": code_ok},
    )
    assert r3.status_code == 200, r3.text
    assert r3.json()["status"] == "resolved"


# ======================================================================
# Fix iteration 4 — bypass paths + loader + liveness
# ======================================================================


def test_deploy_buy_list_excludes_open_remediation_tickers(
    integrity_session,
) -> None:
    """FAILS if inbox buy list still includes a ticker with open remediation."""
    from datetime import date

    from argosy.services.decision_integrity.actionable import (
        filter_tickers_with_open_remediations,
    )
    from argosy.services.deployment_advisor import (
        DeploymentLine,
        DeploymentPlan,
        DeploymentTier,
        EstateTag,
    )
    from argosy.services.deployment_funnel.canonical import deploy_plan_to_buy_list

    sess, _run_id = integrity_session
    persist_remediation_requests(
        sess,
        user_id="ariel",
        requests=[
            RemediationRequest(
                kind="data_integrity",
                target_role="fundamentals",
                reason="break",
                ticker="BADX",
            )
        ],
    )
    sess.commit()

    blocked = filter_tickers_with_open_remediations(
        sess, user_id="ariel", tickers=["BADX", "CSPX"],
    )
    assert blocked == {"BADX"}

    estate = EstateTag(domicile="Global", status="estate_safe", note="")
    line_bad = DeploymentLine(
        symbol="BADX", type="Stock", amount_usd=1000.0, timing="now",
        is_new=True, tier="high", horizon="5-10yr", estate=estate,
        cap_note="", net_of_tax_caveat="", rationale="bad", cites=(),
    )
    line_ok = DeploymentLine(
        symbol="CSPX", type="ETF", amount_usd=1000.0, timing="now",
        is_new=True, tier="core", horizon="10yr+", estate=estate,
        cap_note="", net_of_tax_caveat="", rationale="ok", cites=(),
    )
    empty = lambda n, c: DeploymentTier(n, c, ())
    plan = DeploymentPlan(
        deploy_amount_usd=2000.0,
        as_of=date(2026, 8, 7),
        tiers=(
            empty("reserve", 0.0),
            DeploymentTier("core", 70.0, (line_ok,)),
            empty("medium", 25.0),
            DeploymentTier("high", 5.0, (line_bad,)),
        ),
        us_situs_exposed_usd=0.0,
        us_situs_sanctioned_usd=0.0,
        undeployed_remainder_usd=0.0,
        market_context_age=None,
        caveats=(),
        note="",
    )
    rows = deploy_plan_to_buy_list(
        plan, doc=None, user_id="ariel", blocked_tickers=blocked,
    )
    instruments = {r["instrument"] for r in rows}
    assert "BADX" not in instruments
    assert "CSPX" in instruments


def test_actionable_buy_integrity_blocks_absent_provenance() -> None:
    """Discovery/deploy shared helper — FAILS if BUY bypasses provenance."""
    from argosy.services.decision_integrity.actionable import (
        evaluate_actionable_buy_integrity,
    )

    gate = evaluate_actionable_buy_integrity(
        None,
        user_id="ariel",
        ticker="TRLV",
        fundamentals_fields=None,
        skip_db=True,
    )
    assert gate.block is True
    assert gate.blocked_by == "provenance_unknown"


@pytest.mark.asyncio
async def test_load_analyst_reports_surfaces_remediation_requests(engine: None) -> None:
    """FAILS if _load_analyst_reports drops remediation_requests (blocker 4).

    Drives the REAL loader path used by report-ID API calls.
    """
    import json

    from argosy.api.routes.decisions import _load_analyst_reports
    from argosy.services.decision_integrity.gates import (
        collect_remediation_requests_from_reports,
    )
    from argosy.state import db as db_mod
    from argosy.state.models import AgentReport as AgentReportRow
    from argosy.state.models import User

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    payload = {
        "agent_role": "fundamentals",
        "cited_sources": ["fundamentals/TRLV"],
        "summary": "broken",
        "remediation_requests": [
            {
                "kind": "data_integrity",
                "target_role": "fundamentals",
                "reason": "market_cap/price divergence >10%",
                "ticker": "TRLV",
            }
        ],
    }
    async with db_mod.get_session() as session:
        row = AgentReportRow(
            user_id="ariel",
            agent_role="fundamentals",
            decision_id="1",
            prompt_hash="h",
            response_text=json.dumps(payload),
            tokens_in=1,
            tokens_out=1,
            cost_usd=0.0,
            model="test",
            confidence="LOW",
        )
        session.add(row)
        await session.commit()
        rid = row.id

    reports = await _load_analyst_reports("ariel", [rid])
    assert len(reports) == 1
    dumped = reports[0].output.model_dump()
    assert dumped.get("remediation_requests"), dumped
    collected = collect_remediation_requests_from_reports(reports)
    assert len(collected) >= 1
    assert collected[0].kind == "data_integrity"

    gate = evaluate_green_light_integrity(
        None,
        user_id="ariel",
        ticker="TRLV",
        fundamentals_fields=_trlv_fresh_fields(),
        analyst_reports=reports,
        skip_db=True,
    )
    assert gate.block is True
    assert gate.blocked_by == "open_remediation"


def test_finnhub_series_period_helper() -> None:
    from argosy.adapters.data.finnhub_adapter import (
        _latest_quarterly_period_from_series,
    )

    assert _latest_quarterly_period_from_series(None) is None
    assert (
        _latest_quarterly_period_from_series(
            {
                "quarterly": {
                    "revenue": [
                        {"period": "2026-03-31", "v": 1},
                        {"period": "2026-06-30", "v": 2},
                    ]
                }
            }
        )
        == "2026-06-30"
    )
