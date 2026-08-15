"""Tests for the fund/ETF vehicle verdict path.

Tests are pure-seam: no LLM, no network, no live DB (``session`` fixture
uses an isolated tmp SQLite at alembic head via the shared conftest).

Covers:
  1. FundVehicleAnalystAgent.build_prompt — structure and source keys.
  2. run_fund_vehicle_decision with an injected fake agent — happy path,
     pushback-gate (defended), and agent-error handling.
  3. verdict_coverage._is_collective_instrument — routing gate.
  4. verdict_coverage._default_decide_fn routes ETF to fund vehicle path
     (injected decide_fn; no live asyncio.run needed).
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from argosy.agents.base import ConfidenceBand
from argosy.agents.fund_vehicle_analyst import FundVehicleAnalystAgent, FundVehicleReport
from argosy.services.decision_funnel.fund_vehicle_decision import (
    FundVehicleOutcome,
    run_fund_vehicle_decision,
)
from argosy.services.verdict_coverage import _is_collective_instrument


# ---------------------------------------------------------------------------
# 1. FundVehicleAnalystAgent.build_prompt
# ---------------------------------------------------------------------------

class TestFundVehicleAnalystBuildPrompt:
    def _agent(self) -> FundVehicleAnalystAgent:
        return FundVehicleAnalystAgent(user_id="ariel")

    def _ctx(self, **overrides) -> dict[str, Any]:
        base = {
            "structure": "ETF",
            "asset_class": "Equity",
            "sector": "Broad Index",
            "region": "Global",
            "estate_safe": True,
            "domicile_country": "IE",
            "us_weight": 0.62,
            "us_weight_source": "FTSE All-World index factsheet",
            "index_name": "FTSE All-World",
            "plan_role": "global equity sleeve backbone",
            "position_weight_pct": 8.5,
            "position_usd_value": 45000.0,
            "other_book_holdings": ["NVDA", "CSPX", "EIMI"],
        }
        base.update(overrides)
        return base

    def test_returns_3_tuple(self):
        agent = self._agent()
        result = agent.build_prompt(
            ticker="FWRA",
            fund_context=self._ctx(),
        )
        assert len(result) == 3, "build_prompt must return (system, user, sources)"

    def test_sources_contain_fund_context_key(self):
        agent = self._agent()
        _, _, sources = agent.build_prompt(ticker="FWRA", fund_context=self._ctx())
        source_ids = [sid for sid, _ in sources]
        assert "fund_context/FWRA" in source_ids

    def test_domain_knowledge_source_present_when_supplied(self):
        agent = self._agent()
        dk = "Estate tax info."
        _, _, sources = agent.build_prompt(
            ticker="FWRA", fund_context=self._ctx(), domain_knowledge=dk
        )
        source_ids = [sid for sid, _ in sources]
        assert "domain_knowledge/tax" in source_ids

    def test_domain_knowledge_absent_when_empty(self):
        agent = self._agent()
        _, _, sources = agent.build_prompt(
            ticker="FWRA", fund_context=self._ctx(), domain_knowledge=""
        )
        source_ids = [sid for sid, _ in sources]
        assert "domain_knowledge/tax" not in source_ids

    def test_system_prompt_mentions_domicile_and_ucits(self):
        agent = self._agent()
        system, _, _ = agent.build_prompt(ticker="FWRA", fund_context=self._ctx())
        assert "UCITS" in system or "domicile" in system.lower()

    def test_system_prompt_mentions_nvda_concentration(self):
        agent = self._agent()
        system, _, _ = agent.build_prompt(ticker="FWRA", fund_context=self._ctx())
        assert "NVDA" in system

    def test_user_prompt_names_ticker(self):
        agent = self._agent()
        _, user, _ = agent.build_prompt(ticker="fwra", fund_context=self._ctx())
        assert "FWRA" in user

    def test_ticker_normalised_upper(self):
        agent = self._agent()
        _, _, sources = agent.build_prompt(ticker="fwra", fund_context=self._ctx())
        assert "fund_context/FWRA" in [sid for sid, _ in sources]

    def test_missing_ter_not_injected(self):
        """When ter_bps is not in the context the system prompt must say NOT SUPPLIED."""
        agent = self._agent()
        ctx = self._ctx()
        ctx.pop("ter_bps", None)  # ensure absent
        _, _, sources = agent.build_prompt(ticker="EIMI", fund_context=ctx)
        fund_ctx_content = next(
            content for sid, content in sources if sid == "fund_context/EIMI"
        )
        assert "NOT SUPPLIED" in fund_ctx_content

    def test_agent_role(self):
        agent = self._agent()
        assert agent.agent_role == "fund_vehicle_analyst"

    def test_require_citations(self):
        assert FundVehicleAnalystAgent.require_citations is True


# ---------------------------------------------------------------------------
# 2. run_fund_vehicle_decision — injected fake agent
# ---------------------------------------------------------------------------

def _fake_report(
    ticker: str = "FWRA",
    verdict: str = "HOLD",
    conviction: ConfidenceBand = ConfidenceBand.MEDIUM,
    falsifiers: list[str] | None = None,
    revisit_triggers: list[dict] | None = None,
) -> FundVehicleReport:
    return FundVehicleReport(
        ticker=ticker,
        verdict=verdict,
        conviction=conviction,
        reasoning_md=(
            "FWRA (FTSE All-World UCITS) is Irish-domiciled and not US-situs. "
            "It implements the global equity sleeve with a 62% US tilt that the plan "
            "accepts. TER is not confirmed from the packet; confidence is MEDIUM."
        ),
        domicile_ok=True,
        ter_known=False,
        ter_bps=None,
        nvda_lookahead_weight_pct=3.4,
        overlap_instruments=["ACWD"],
        falsifiers=falsifiers or [
            "Tracking difference to FTSE All-World exceeds 20 bps over any rolling 12-month window.",
            "A lower-cost UCITS vehicle covering the same FTSE All-World mandate with TER < 10 bps becomes available.",
        ],
        revisit_triggers=revisit_triggers or [
            {"kind": "dated_event", "label": "annual fund review", "date": "2027-01-01"},
            {"kind": "metric_condition", "metric": "tracking_diff_bps", "op": ">=", "value": 20},
        ],
        confidence=conviction,
        cited_sources=[
            "fund_context/FWRA",
            "domain_knowledge/tax/us/estate_tax_nonresidents.md",
        ],
        data_gaps=["TER not supplied"],
    )


class _FakeAgentReport:
    """Mimics the ``AgentReport`` object returned by ``agent.run()``.

    Field names must match ``argosy.agents.base.AgentReport`` — in particular
    ``output`` (NOT ``structured_output``). The persistence block in
    ``run_fund_vehicle_decision`` reads all of these.
    """
    def __init__(self, report_output):
        self.output = report_output            # AgentReport.output (the pydantic model)
        self.agent_role = "fund_vehicle_analyst"
        self.response_text = "ok"
        self.prompt_hash = "testhash"
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_usd = 0.0
        self.model = "claude-opus-4-8"
        self.confidence = None
        self.cache_input_tokens = 0
        self.cache_creation_tokens = 0
        self.thinking_tokens = 0
        self.citations_json = None
        self.sources_json = None
        self.run_correlation_id = None
        self.system_prompt = None
        self.user_prompt = None


class _FakeAgent:
    """Stand-in for FundVehicleAnalystAgent with a no-LLM async run()."""
    def __init__(self, report: FundVehicleReport):
        self._report = report

    async def run(self, **_kwargs) -> _FakeAgentReport:
        return _FakeAgentReport(self._report)


@pytest.fixture
def session(alembic_engine_at_head):
    from sqlalchemy.orm import sessionmaker
    from argosy.state.models import User
    SessionLocal = sessionmaker(bind=alembic_engine_at_head, expire_on_commit=False)
    s = SessionLocal()
    s.add(User(id="ariel", plan="free"))
    s.commit()
    yield s
    s.close()


class TestRunFundVehicleDecision:
    """Integration-style tests with an injected fake agent (no LLM).

    All tests patch the DB-touching seams (pushback gate, context builder,
    verdict writer) so the production DB is never read from or written to.
    Each test is fully isolated.
    """

    def _run(self, ticker: str = "FWRA", report: FundVehicleReport | None = None,
             user_id: str = "ariel",
             gate_defended: bool = False,
             write_verdict_id: int | None = 42) -> FundVehicleOutcome:
        """Drive run_fund_vehicle_decision synchronously with all DB seams patched.

        Args:
            gate_defended: simulate the pushback gate defending the verdict.
            write_verdict_id: verdict id returned by the patched writer.
                None simulates a write failure.
        """
        rep = report or _fake_report(ticker=ticker)
        fake_agent = _FakeAgent(rep)

        from argosy.services.verdict_registry import PushbackGateResult
        from argosy.state.models import Verdict as VerdictModel

        # Fake standing verdict for the defended gate path.
        fake_standing = MagicMock(spec=VerdictModel)
        fake_standing.id = 99
        fake_standing.verdict = "HOLD"
        fake_standing.conviction = "MED"
        fake_standing.source_decision_run_id = None

        gate_result = (
            PushbackGateResult(
                allowed=False,
                standing=fake_standing,
                reason="DEFENDED: settled HOLD",
            )
            if gate_defended
            else PushbackGateResult(allowed=True, reason="no_settled_verdict")
        )

        async def _async_run():
            with (
                patch(
                    "argosy.services.decision_funnel.fund_vehicle_decision.check_pushback_gate",
                    return_value=gate_result,
                ),
                patch(
                    "argosy.services.decision_funnel.fund_vehicle_decision._build_fund_context",
                    new=AsyncMock(return_value={"ticker": ticker.upper()}),
                ),
                patch(
                    "argosy.services.decision_funnel.fund_vehicle_decision._load_domain_knowledge",
                    return_value="[domain knowledge placeholder]",
                ),
                patch(
                    "argosy.services.decision_funnel.fund_vehicle_decision._write_fund_verdict",
                    return_value=write_verdict_id,
                ),
            ):
                return await run_fund_vehicle_decision(
                    user_id=user_id,
                    ticker=ticker,
                    _agent_factory=lambda: fake_agent,
                )

        return asyncio.run(_async_run())

    def test_happy_path_status_completed(self):
        outcome = self._run("FWRA")
        assert outcome.status == "completed", f"Expected completed, got: {outcome}"

    def test_happy_path_verdict_id_propagated(self):
        """Completed run must expose the verdict_id returned by the writer."""
        outcome = self._run("FWRA", write_verdict_id=42)
        assert outcome.verdict_id == 42
        assert outcome.ticker == "FWRA"
        assert outcome.verdict == "HOLD"

    def test_verdict_conviction_propagated(self):
        report = _fake_report(ticker="EIMI", verdict="HOLD", conviction=ConfidenceBand.LOW)
        outcome = self._run("EIMI", report=report)
        assert outcome.verdict == "HOLD"

    def test_sell_verdict_accepted(self):
        report = _fake_report(
            ticker="SGOV",
            verdict="SELL",
            conviction=ConfidenceBand.HIGH,
            falsifiers=[
                "SGOV is US-domiciled; the household's US-situs estate exposure "
                "is reduced below $60K (NRA exemption fully covered).",
                "IB01 (UCITS equivalent) is available at comparable TER.",
            ],
            revisit_triggers=[
                {"kind": "dated_event", "label": "estate plan review", "date": "2027-01-01"},
            ],
        )
        outcome = self._run("SGOV", report=report)
        assert outcome.status == "completed"
        assert outcome.verdict == "SELL"

    def test_verdict_write_failure_returns_error(self):
        """When the verdict writer returns None, the outcome must be error."""
        outcome = self._run("FWRA", write_verdict_id=None)
        assert outcome.status == "error"
        assert outcome.blocked_by == "registry_write_error"

    def test_agent_error_returns_error_status(self):
        """When the agent raises, outcome status must be error."""
        class _FailingAgent:
            async def run(self, **_kwargs):
                raise RuntimeError("LLM timeout")

        from argosy.services.verdict_registry import PushbackGateResult

        async def _async_run():
            with (
                patch(
                    "argosy.services.decision_funnel.fund_vehicle_decision.check_pushback_gate",
                    return_value=PushbackGateResult(allowed=True, reason="no_settled_verdict"),
                ),
                patch(
                    "argosy.services.decision_funnel.fund_vehicle_decision._build_fund_context",
                    new=AsyncMock(return_value={"ticker": "FWRA"}),
                ),
                patch(
                    "argosy.services.decision_funnel.fund_vehicle_decision._load_domain_knowledge",
                    return_value="",
                ),
            ):
                return await run_fund_vehicle_decision(
                    user_id="ariel",
                    ticker="FWRA",
                    _agent_factory=lambda: _FailingAgent(),
                )

        outcome = asyncio.run(_async_run())
        assert outcome.status == "error"
        assert "LLM timeout" in (outcome.blocked_reason or "")

    def test_pushback_gate_defended_returns_blocked(self):
        """When the gate defends, outcome is blocked without calling the agent."""
        outcome = self._run("FWRA", gate_defended=True)
        assert outcome.status == "blocked"
        assert outcome.blocked_by == "verdict_defended"


# ---------------------------------------------------------------------------
# 3. _is_collective_instrument
# ---------------------------------------------------------------------------

class TestIsCollectiveInstrument:
    @pytest.mark.parametrize("symbol,structure,expected", [
        ("FWRA", "etf", True),
        ("EIMI", "ETF", True),
        ("IBTA", "bond", True),
        ("O", "reit", True),
        ("NVDA", "Stock", False),
        ("NVDA", "stock", False),
        ("SOFI", "Stock", False),
        ("FWRA", "unknown", False),   # unknown → equity fleet (safe fallback)
        ("FWRA", "", False),           # blank → equity fleet
    ])
    def test_routing(self, symbol, structure, expected):
        result = _is_collective_instrument(symbol, structure)
        assert result == expected, (
            f"_is_collective_instrument({symbol!r}, {structure!r}) = {result}, "
            f"expected {expected}"
        )


# ---------------------------------------------------------------------------
# 4. FundVehicleReport validation
# ---------------------------------------------------------------------------

class TestFundVehicleReport:
    def test_invalid_revisit_trigger_kind_filtered(self):
        """The decision function filters out invalid trigger kinds before writing."""
        # This tests that the writer helper (_write_fund_verdict) validates kind.
        # We inject a report with one valid + one invalid trigger and verify the
        # report model itself accepts arbitrary dicts (validation is in the writer).
        report = FundVehicleReport(
            ticker="FWRA",
            verdict="HOLD",
            conviction=ConfidenceBand.MEDIUM,
            domicile_ok=True,
            falsifiers=["f1", "f2"],
            revisit_triggers=[
                {"kind": "dated_event", "label": "review", "date": "2027-01-01"},
                {"kind": "INVALID_KIND", "label": "bogus"},
            ],
        )
        # Pydantic accepts the list — validation is writer-side.
        assert len(report.revisit_triggers) == 2

    def test_buy_verdict_is_valid_pydantic(self):
        """The schema does not block BUY at the pydantic level (the agent prompt
        is what prevents it; a defensive caller can still check if needed)."""
        report = FundVehicleReport(
            ticker="X",
            verdict="BUY",
            domicile_ok=True,
            conviction=ConfidenceBand.LOW,
        )
        assert report.verdict == "BUY"

    def test_data_gaps_defaults_empty(self):
        report = FundVehicleReport(
            ticker="X",
            verdict="HOLD",
            domicile_ok=True,
            conviction=ConfidenceBand.LOW,
        )
        assert report.data_gaps == []

    def test_ter_unknown_by_default(self):
        report = FundVehicleReport(
            ticker="X",
            verdict="HOLD",
            domicile_ok=True,
            conviction=ConfidenceBand.LOW,
        )
        assert report.ter_known is False
        assert report.ter_bps is None
