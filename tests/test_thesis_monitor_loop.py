"""ThesisMonitorLoop tests — fully seam-injected (no live feed / LLM / DB)."""

from __future__ import annotations

import pytest

from argosy.agents.thesis_monitor import HoldingThesisAssessment, ThesisMonitorReport
from argosy.orchestrator.loops.thesis_monitor import ThesisMonitorLoop


class _FakeSession:
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
