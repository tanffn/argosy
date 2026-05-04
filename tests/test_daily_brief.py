"""Daily Brief end-to-end test with all four agents mocked.

Verifies:
  - all four reports persist into `daily_briefs`
  - summary_text mentions the regime + breach count
  - WebSocket event `daily_brief.ready` fires
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from argosy.agents.base import ModelCall
from argosy.agents.concentration_analyst import ConcentrationAnalystAgent
from argosy.agents.macro_analyst import MacroAnalystAgent
from argosy.agents.news_analyst import NewsAnalystAgent
from argosy.agents.plan_critique import PlanCritiqueAgent
from argosy.api import events
from argosy.orchestrator.loops.base import LoopSchedule
from argosy.orchestrator.loops.daily_brief import DailyBriefInputs, DailyBriefLoop
from argosy.state import db as db_mod
from argosy.state.models import DailyBrief, User


def _mock(agent_cls, canned: dict):
    class _M(agent_cls):  # type: ignore[misc, valid-type]
        async def _call_model(self, *, system: str, user: str) -> ModelCall:
            return ModelCall(
                text=json.dumps(canned),
                tokens_in=100,
                tokens_out=200,
                model=self.model,
            )
    return _M


_NEWS_CANNED = {
    "per_ticker": {
        "NVDA": [
            {
                "ticker": "NVDA",
                "title": "NVDA earnings beat",
                "url": "https://x/1",
                "source": "Reuters",
                "summary": "Beat by 5%",
                "materiality": 0.6,
            }
        ]
    },
    "materiality_scores": {"NVDA": 0.6},
    "top_line": "NVDA earnings beat the street.",
    "confidence": "MEDIUM",
    "cited_sources": ["https://x/1"],
}

_MACRO_CANNED = {
    "regime": "neutral",
    "drivers": ["VIX 15", "10Y 4.2%"],
    "key_metrics": {"vix": 15.0, "fred_10y": 4.2},
    "summary": "Calm waters.",
    "confidence": "MEDIUM",
    "cited_sources": ["fred:VIXCLS"],
}

_CONC_CANNED = {
    "breaches": [
        {
            "category": "NVDA",
            "actual_pct": 68.0,
            "cap_pct": 25.0,
            "severity": "breach",
            "note": "Way over cap.",
        }
    ],
    "deltas_vs_target": {"NVDA": 53.0},
    "nvda_pace": {
        "shares_sold_ytd": 2000,
        "target_shares_ytd": 4000,
        "delta_shares": -2000,
        "on_track": False,
    },
    "summary": "NVDA over cap.",
    "confidence": "HIGH",
    "cited_sources": ["plan:v2.0", "tsv:may2026"],
}

_PLAN_CRITIQUE_CANNED = {
    "plan_label": "Jacobs_Wealth_Plan v2.0",
    "snapshot_label": "daily_brief",
    "overall_summary": "NVDA still 68% — RED.",
    "confidence": "MEDIUM",
    "cited_sources": ["domain_knowledge/tax/israel/retirement/section_102.md"],
    "findings": [
        {
            "plan_item_ref": "Concentration target — NVDA 15%",
            "severity": "RED",
            "topic": "Concentration",
            "summary": "NVDA still 68%.",
            "evidence": ["Snapshot shows 11,471 NVDA shares."],
            "cited_sources": ["domain_knowledge/tax/israel/retirement/section_102.md"],
            "recommended_action": None,
        }
    ],
}


def _gather_inputs(_user_id: str) -> DailyBriefInputs:
    return DailyBriefInputs(
        user_id=_user_id,
        tickers=["NVDA"],
        news_payload={"NVDA": [{"headline": "NVDA earnings beat"}]},
        macro_snapshot={"vix": 15.0, "fred_10y": 4.2},
        positions_summary="NVDA 11,471 shares; total liquid $3.36M",
        plan_targets={"NVDA": 15.0},
        nvda_shares_sold_ytd=2000,
        nvda_target_shares_ytd=4000,
        plan_label="Jacobs_Wealth_Plan v2.0",
        plan_markdown="# Plan\n\nNVDA target 15%.\n",
    )


@pytest.mark.asyncio
async def test_daily_brief_end_to_end(engine: None) -> None:
    events._reset_for_tests()

    # Seed the user row so daily_briefs.user_id FK is satisfied.
    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    # Subscribe to events and capture them. We use the queue directly so
    # the test never races the listener-startup.
    received: list[str] = []
    sub_ctx = events.subscribe()
    q = await sub_ctx.__aenter__()

    loop = DailyBriefLoop(
        schedule=LoopSchedule(cron="0 9 * * *"),
        user_id="ariel",
        news_agent_factory=lambda: _mock(NewsAnalystAgent, _NEWS_CANNED)(user_id="ariel"),
        macro_agent_factory=lambda: _mock(MacroAnalystAgent, _MACRO_CANNED)(user_id="ariel"),
        concentration_agent_factory=lambda: _mock(
            ConcentrationAnalystAgent, _CONC_CANNED
        )(user_id="ariel"),
        plan_critique_agent_factory=lambda: _mock(
            PlanCritiqueAgent, _PLAN_CRITIQUE_CANNED
        )(user_id="ariel"),
        gather_inputs=_gather_inputs,
    )

    fixed_now = datetime(2026, 5, 2, 9, 0, tzinfo=timezone.utc)
    await loop.tick(now=lambda: fixed_now)

    # Drain any queued events.
    while not q.empty():
        received.append(q.get_nowait())
    await sub_ctx.__aexit__(None, None, None)

    # Verify daily_briefs row.
    async with db_mod.get_session() as session:
        row = (await session.execute(select(DailyBrief))).scalar_one()
        assert row.user_id == "ariel"
        # SQLite returns tz-naive; compare on the ymdhms tuple instead.
        assert (row.run_at.year, row.run_at.month, row.run_at.day, row.run_at.hour) == (
            fixed_now.year,
            fixed_now.month,
            fixed_now.day,
            fixed_now.hour,
        )
        # Each report JSON must round-trip.
        news_obj = json.loads(row.news_report_json)
        assert news_obj["top_line"].startswith("NVDA")
        macro_obj = json.loads(row.macro_report_json)
        assert macro_obj["regime"] == "neutral"
        conc_obj = json.loads(row.concentration_report_json)
        assert conc_obj["breaches"][0]["category"] == "NVDA"
        plan_obj = json.loads(row.plan_delta_json)
        assert plan_obj["findings"][0]["severity"] == "RED"
        assert "DAILY BRIEF" in row.summary_text
        assert "1 breach" in row.summary_text or "breach" in row.summary_text

    # Verify the WebSocket event fired.
    assert any("daily_brief.ready" in m for m in received), (
        f"Expected daily_brief.ready event, got: {received!r}"
    )


# ----------------------------------------------------------------------
# _default_gather_inputs — investor-event adapter graceful degradation.
# Covers the Phase 4 review fix where MissingDataSourceError was used
# inside the inner per-ticker try/except but never imported at module
# scope. Without the import, an outage on the *first* ticker turns into
# a NameError swallowed by the outer ``except Exception``, dropping the
# entire section instead of degrading per-ticker.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_gather_inputs_form4_outage_degrades(
    monkeypatch: pytest.MonkeyPatch, engine: None
) -> None:
    from argosy.adapters import MissingDataSourceError
    from argosy.adapters.data import sec_form4_adapter, tipranks_adapter
    from argosy.orchestrator.loops import daily_brief as db_loop
    from argosy.state.models import User

    async with db_mod.get_session() as session:
        session.add(User(id="ariel"))
        await session.commit()

    # Pretend a TSV exists with two tickers; we don't actually parse a
    # file — we monkeypatch the loader to return a synthetic snapshot.
    class _FakePos:
        def __init__(self, t: str) -> None:
            self.ticker = t
            self.quantity = 1
            self.market_value = 0
            self.value = 0
            self.account = ""

    class _FakeSnap:
        positions = [_FakePos("NVDA"), _FakePos("AAPL")]

    monkeypatch.setattr(db_loop, "_find_latest_tsv", lambda: "fake.tsv")
    import argosy.ingest.tsv as ingest_tsv
    monkeypatch.setattr(ingest_tsv, "parse_portfolio_tsv", lambda _p: _FakeSnap())

    # Failing Form 4 adapter — first ticker outage must NOT abort the
    # loop; subsequent tickers must continue to be attempted.
    class _OutageForm4:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get_recent_form4_for_ticker(
            self, ticker: str, *, days: int = 30
        ) -> list[dict]:
            self.calls.append(ticker)
            raise MissingDataSourceError(f"simulated SEC outage for {ticker}")

    outage_form4 = _OutageForm4()
    monkeypatch.setattr(
        sec_form4_adapter, "SecForm4Adapter", lambda: outage_form4
    )

    # Failing TipRanks adapter — same pattern.
    class _OutageTipRanks:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get_analyst_consensus(self, ticker: str) -> dict:
            self.calls.append(ticker)
            raise MissingDataSourceError(f"simulated TipRanks outage for {ticker}")

    outage_tr = _OutageTipRanks()
    monkeypatch.setattr(tipranks_adapter, "TipRanksAdapter", lambda: outage_tr)

    inputs = await db_loop._default_gather_inputs("ariel")

    # Both tickers should have been attempted, despite the first one
    # raising. The empty per-ticker dict is the graceful-degradation
    # outcome — neither a crash nor silent abort after the first miss.
    assert outage_form4.calls == ["AAPL", "NVDA"]
    assert outage_tr.calls == ["AAPL", "NVDA"]
    assert inputs.insider_activity == {}
    assert inputs.analyst_signals == {}
    # Other fields should still be populated from the fake snapshot.
    assert sorted(inputs.tickers) == ["AAPL", "NVDA"]
