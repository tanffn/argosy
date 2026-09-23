"""ThesisMonitorLoop tests — fully seam-injected (no live feed / LLM / DB)."""

from __future__ import annotations

import json
from datetime import UTC

import pytest

from argosy.agents.base import ModelCall
from argosy.agents.thesis_monitor import (
    HoldingThesisAssessment,
    ThesisMonitorAgent,
    ThesisMonitorReport,
)
from argosy.orchestrator.loops.thesis_monitor import ThesisMonitorLoop, _price_summary


def test_price_summary_reduces_eod_bars() -> None:
    # Bars are dicts keyed 'Close' (the YFinanceAdapter.get_eod_prices shape).
    bars = [{"Close": float(c)} for c in range(100, 100 + 80)]  # 80 rising days
    s = _price_summary(bars)
    assert s["last"] == 179.0
    assert s["ret_1m_pct"] == pytest.approx(100.0 * (179 / 158 - 1), abs=0.1)  # 21d back
    assert s["off_52w_high_pct"] == 0.0  # last == max (monotone rising)
    assert _price_summary([]) == {}
    assert _price_summary([{"no_close": 1}]) == {}


def test_news_outage_uses_existing_yahoo_fallback_and_normalizes_sec_symbol(monkeypatch):
    from datetime import datetime
    from argosy.adapters.data.finnhub_adapter import FinnhubAdapter
    from argosy.adapters.data.yfinance_adapter import YFinanceAdapter
    from argosy.adapters.data.sec_form4_adapter import SecForm4Adapter
    from argosy.services.stock_decision import fetchers
    from argosy.orchestrator.loops.thesis_monitor import default_gather_feeds

    async def unavailable(*args, **kwargs):
        raise RuntimeError("Finnhub not configured")

    async def prices(*args, **kwargs):
        return {"BRK-B": [{"Close": 500}]}

    seen = []

    async def insider(self, symbol, **kwargs):
        seen.append(symbol)
        return []

    monkeypatch.setattr(FinnhubAdapter, "get_company_news", unavailable)
    monkeypatch.setattr(YFinanceAdapter, "get_eod_prices", prices)
    monkeypatch.setattr(SecForm4Adapter, "get_recent_form4_for_ticker", insider)
    monkeypatch.setattr(fetchers, "_yahoo_news", lambda *a, **kw: "source=yahoo; 2026-09-11: Company update")
    bundle = default_gather_feeds({"ticker": "BRK/B"}, now=datetime.now(UTC))
    assert bundle["news"][0]["source"] == "yahoo"
    assert "Company update" in bundle["news"][0]["headline"]
    assert bundle["feed_errors"] == []
    assert seen == ["BRK-B"]


class _FakeSession:
    def add(self, row) -> None:
        pass

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeAgentReport:
    def __init__(self, output) -> None:
        self.output = output


class _FakeAgent:
    def __init__(self, assessments) -> None:
        self._assessments = assessments

    async def run(self, *, bundles):  # noqa: ARG002 — bundles unused in fake
        return _FakeAgentReport(
            ThesisMonitorReport(assessments=self._assessments, overall_summary="x")
        )


def _loop(*, holdings, assessments, write_calls):
    def _write_fn(session, user_id, a, *, now):  # noqa: ARG001
        write_calls.append(a.ticker)
        return len(write_calls)

    return ThesisMonitorLoop(
        user_id="ariel",
        session_factory=lambda: _FakeSession(),
        holdings_fn=lambda *_a, **_k: holdings,
        gather_fn=lambda h, *, now: {**h, "news": [], "insider": []},
        agent_factory=lambda: _FakeAgent(assessments),
        write_fn=_write_fn,
    )


@pytest.mark.real_seam
@pytest.mark.asyncio
async def test_real_agent_dispatch_flows_through_loop(monkeypatch) -> None:
    """Use the real loop and agent; replace only the external LLM call."""
    payload = {
        "assessments": [{
            "ticker": "O",
            "thesis_status": "broken",
            "severity": "critical",
            "rationale_md": "The dividend was suspended.",
            "signals": ["dividend suspension"],
            "suggested_action": "reassess_thesis",
            "confidence": "HIGH",
            "cited_sources": ["feed/O"],
        }],
        "overall_summary": "One thesis-level change.",
        "confidence": "HIGH",
        "cited_sources": ["feed/O"],
    }

    async def fake_call(self, *, system, user, **kwargs):
        assert "feed/O" in user and "thesis" in system.lower()
        return ModelCall(
            text=json.dumps(payload), tokens_in=10, tokens_out=10, model="test-model"
        )

    monkeypatch.setattr(ThesisMonitorAgent, "_call_model", fake_call)
    writes: list[str] = []

    def write_fn(session, user_id, assessment, *, now):
        writes.append(assessment.ticker)
        return len(writes)

    loop = ThesisMonitorLoop(
        user_id="ariel",
        session_factory=lambda: _FakeSession(),
        holdings_fn=lambda *_a, **_k: [{"ticker": "O", "weight_pct": 3.0}],
        gather_fn=lambda h, *, now: {**h, "news": [], "insider": []},
        agent_factory=lambda: ThesisMonitorAgent(user_id="ariel"),
        write_fn=write_fn,
    )
    summary = await loop.tick()
    assert summary["escalated"] == 1
    assert writes == ["O"]


@pytest.mark.asyncio
async def test_only_thesis_changes_escalate() -> None:
    write_calls: list[str] = []
    loop = _loop(
        holdings=[{"ticker": "NVDA", "weight_pct": 12.0}, {"ticker": "O", "weight_pct": 3.0}],
        assessments=[
            HoldingThesisAssessment(ticker="NVDA", thesis_status="intact", severity="info"),
            HoldingThesisAssessment(
                ticker="O", thesis_status="broken", severity="critical",
                rationale_md="dividend cut", suggested_action="reassess_thesis"),
        ],
        write_calls=write_calls,
    )
    summary = await loop.tick()
    assert summary["assessed"] == 2
    assert summary["escalated"] == 1  # only O (broken); NVDA intact is skipped
    assert summary["flags_written"] == 1
    assert write_calls == ["O"]


@pytest.mark.asyncio
async def test_missing_assessment_and_feed_failure_are_not_green():
    from argosy.services.jobs.summary_status import derive_run_status
    loop = _loop(holdings=[{"ticker": "A"}, {"ticker": "B"}],
                 assessments=[HoldingThesisAssessment(ticker="A")], write_calls=[])
    loop._gather_fn = lambda h, **kwargs: {**h, "feed_errors": ["company news unavailable"]}
    summary = await loop.tick()
    assert derive_run_status(summary)[0] == "error"
    assert "B: model returned no assessment" in summary["errors"]
    assert "A: company news unavailable" in summary["errors"]


@pytest.mark.asyncio
async def test_weakened_escalates_intact_and_strengthened_do_not() -> None:
    write_calls: list[str] = []
    loop = _loop(
        holdings=[{"ticker": "A"}, {"ticker": "B"}, {"ticker": "C"}],
        assessments=[
            HoldingThesisAssessment(ticker="A", thesis_status="weakened", severity="warning"),
            HoldingThesisAssessment(ticker="B", thesis_status="strengthened", severity="info"),
            HoldingThesisAssessment(ticker="C", thesis_status="intact", severity="info"),
        ],
        write_calls=write_calls,
    )
    summary = await loop.tick()
    assert summary["escalated"] == 1 and write_calls == ["A"]


@pytest.mark.asyncio
async def test_weakened_at_info_severity_does_not_escalate() -> None:
    # A weakened/broken status at info severity is NOT actionable (no proposal).
    write_calls: list[str] = []
    loop = _loop(
        holdings=[{"ticker": "A"}, {"ticker": "B"}],
        assessments=[
            HoldingThesisAssessment(ticker="A", thesis_status="weakened", severity="info"),
            HoldingThesisAssessment(ticker="B", thesis_status="broken", severity="info"),
        ],
        write_calls=write_calls,
    )
    summary = await loop.tick()
    assert summary["escalated"] == 0 and write_calls == []


@pytest.mark.asyncio
async def test_no_individual_holdings_skips() -> None:
    write_calls: list[str] = []
    loop = _loop(holdings=[], assessments=[], write_calls=write_calls)
    summary = await loop.tick()
    assert summary.get("skipped_reason") == "no_individual_holdings"
    assert summary["assessed"] == 0 and write_calls == []


# ---------------------------------------------------------------------------
# 2026-07-08 tracking-state audit — FIX 1 (exit triggers reach the monitoring
# prompt) + FIX 4 (open set_watchlist proposals get a consumer).
# ---------------------------------------------------------------------------

def _sqlite_session(tmp_path, name="tm.db"):
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    from argosy.state.models import Base, User

    engine = sa.create_engine(
        f"sqlite:///{tmp_path / name}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    db.add(User(id="ariel", plan="free"))
    db.commit()
    return db, factory


def _watchlist_row(ticker, *, status="open", payload_extra=None, now=None):
    import json
    from datetime import datetime, timedelta

    from argosy.state.models import ActionProposal

    now = now or datetime(2026, 7, 8, tzinfo=UTC)
    payload = {"ticker": ticker, "watch_kind": "catalyst", **(payload_extra or {})}
    return ActionProposal(
        user_id="ariel",
        summary=f"Watch {ticker}",
        rationale_md="watch it",
        suggested_payload=json.dumps(payload),
        severity="info",
        surfaced_at=now - timedelta(days=20),
        expires_at=now + timedelta(days=1),
        status=status,
        kind="set_watchlist",
        dedup_key=f"watch:{ticker}:{status}",
    )


def test_plan_thesis_map_renders_exit_triggers() -> None:
    from argosy.orchestrator.loops.thesis_monitor import plan_thesis_map
    from argosy.services.target_allocation_doc import (
        AllocationClassDoc,
        AllocationInstrument,
        TargetAllocationDoc,
    )

    doc = TargetAllocationDoc(
        anchor_sigma=0.18, blended_sigma=0.18, nvda_cap_pct=13.0, fi_pct=10.0,
        provenance="test",
        classes=[AllocationClassDoc(
            label="High-growth / high-potential",
            snapshot_category="Individual Stocks",
            sigma_class="high_growth_basket",
            target_pct=5.0,
            rationale="x10 sleeve",
            instruments=[
                AllocationInstrument(
                    symbol="TEM", role="primary", weight_within_class_pct=50.0,
                    rationale="AI diagnostics moat",
                    exit_triggers=["oncology read-out fails"],
                    review_on="2026-09-30",
                ),
                AllocationInstrument(
                    symbol="OKLO", role="primary", weight_within_class_pct=50.0,
                ),
            ],
        )],
        glide=[],
    )
    m = plan_thesis_map(doc)
    assert "EXIT TRIGGERS (recorded invalidation conditions): oncology read-out fails" in m["TEM"]
    assert "Review on: 2026-09-30" in m["TEM"]
    assert "AI diagnostics moat" in m["TEM"]
    assert "EXIT TRIGGERS" not in m["OKLO"]          # no recorded triggers
    assert plan_thesis_map(None) == {}


def test_load_open_watchlist_notes_reads_open_rows_only(tmp_path) -> None:
    from argosy.orchestrator.loops.thesis_monitor import load_open_watchlist_notes

    db, _ = _sqlite_session(tmp_path)
    db.add_all([
        _watchlist_row("TEM", payload_extra={
            "catalyst": "Q3 oncology read-out", "review_on": "2026-09-30",
        }),
        _watchlist_row("GONE", status="rejected"),   # not open -> out
    ])
    db.commit()

    notes = load_open_watchlist_notes(db, "ariel")
    assert set(notes) == {"TEM"}
    assert "watch_kind=catalyst" in notes["TEM"]
    assert "catalyst: Q3 oncology read-out" in notes["TEM"]
    assert "review_on: 2026-09-30" in notes["TEM"]
    assert "judge whether the recorded catalyst has fired" in notes["TEM"]
    db.close()


def test_refresh_watchlist_rows_for_ticker_keeps_row_alive(tmp_path) -> None:
    from datetime import datetime, timedelta

    from argosy.orchestrator.loops.thesis_monitor import (
        refresh_watchlist_rows_for_ticker,
    )
    from argosy.state.models import ActionProposal

    now = datetime(2026, 7, 8, tzinfo=UTC)
    db, _ = _sqlite_session(tmp_path, "refresh.db")
    db.add_all([
        _watchlist_row("TEM", now=now),
        _watchlist_row("OTHER", now=now),
        _watchlist_row("TEM", status="rejected", now=now),  # closed -> untouched
    ])
    db.commit()

    assert refresh_watchlist_rows_for_ticker(db, "ariel", "TEM", now=now) == 1

    rows = db.query(ActionProposal).order_by(ActionProposal.id).all()
    tem_open, other_open, tem_closed = rows

    def _utc(dt):
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt

    assert _utc(tem_open.surfaced_at) == now                       # refreshed
    assert _utc(tem_open.expires_at) >= now + timedelta(days=6)    # expiry floored
    assert _utc(other_open.surfaced_at) == now - timedelta(days=20)  # untouched
    assert _utc(tem_closed.surfaced_at) == now - timedelta(days=20)  # untouched
    db.close()


@pytest.mark.asyncio
async def test_loop_appends_watchlist_notes_to_bundles(tmp_path) -> None:
    """The daily loop is the set_watchlist consumer: open rows' catalyst text
    rides into the agent bundle for their symbols."""
    _db, factory = _sqlite_session(tmp_path, "loop.db")
    seed = factory()
    seed.add(_watchlist_row("TEM", payload_extra={"catalyst": "read-out"}))
    seed.commit()
    seed.close()

    bundles_seen: list[dict] = []

    def _gather(h, *, now):  # noqa: ARG001
        bundles_seen.append(dict(h))
        return {**h, "news": [], "insider": []}

    loop = ThesisMonitorLoop(
        user_id="ariel",
        session_factory=factory,
        holdings_fn=lambda *_a, **_k: [
            {"ticker": "TEM", "weight_pct": 0.3, "plan_thesis": "x10"},
            {"ticker": "NVDA", "weight_pct": 60.0, "plan_thesis": "core"},
        ],
        gather_fn=_gather,
        agent_factory=lambda: _FakeAgent([]),
        write_fn=lambda *a, **k: None,
    )
    summary = await loop.tick()
    by_ticker = {b["ticker"]: b for b in bundles_seen}
    assert "catalyst: read-out" in by_ticker["TEM"]["watchlist"]
    assert "watchlist" not in by_ticker["NVDA"]      # no open row for NVDA
    assert summary["watchlist_notes"] == 1
