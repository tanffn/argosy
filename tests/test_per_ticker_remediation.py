"""Tests for the per-ticker inter-agent remediation flow.

Per [[feedback_agents_talk_to_each_other]] — when an analyst flags a
data-quality issue, the orchestrator dispatches the refresh + re-runs
the requesting analyst BEFORE downstream agents see the stale report.
The fleet resolves internally; the user never sees a "please refresh
and try again" verdict.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from argosy.agents.base import AgentReport, ConfidenceBand
from argosy.agents.remediation import RemediationKind, RemediationRequest
from argosy.orchestrator.flows.per_ticker_remediation import (
    MAX_REMEDIATION_ROUNDS,
    apply_remediations_and_rerun,
)


class _Output(BaseModel):
    """Stand-in for a real analyst output; carries `remediation_requests`."""

    cited_sources: list[str] = []
    note: str = ""
    remediation_requests: list[RemediationRequest] = []


def _report(role: str, *, requests: list[RemediationRequest] | None = None) -> AgentReport:
    return AgentReport(
        agent_role=role,
        user_id="ariel",
        model="claude-sonnet-4-6",
        response_text="{}",
        tokens_in=10,
        tokens_out=10,
        cost_usd=0.0,
        prompt_hash="hash",
        confidence=ConfidenceBand.MEDIUM,
        output=_Output(cited_sources=[f"{role}:source"], remediation_requests=requests or []),
    )


def _request(kind: RemediationKind, role: str, *, ticker: str = "NOW") -> RemediationRequest:
    return RemediationRequest(
        kind=kind, target_role=role, reason=f"stub {kind} request", ticker=ticker,
    )


# ----------------------------------------------------------------------
# No requests → no-op
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_requests_returns_inputs_unchanged() -> None:
    reports = [_report("fundamentals"), _report("news")]
    refresh_calls: list[tuple[str, str]] = []
    rerun_calls: list[str] = []

    async def refresh(kind: str, ticker: str) -> bool:
        refresh_calls.append((kind, ticker))
        return True

    async def rerun(role: str, _r: list[AgentReport]) -> AgentReport | None:
        rerun_calls.append(role)
        return None

    out, unresolved = await apply_remediations_and_rerun(
        reports=reports, user_id="ariel", ticker="NOW", decision_run_id=1,
        rerun_analyst=rerun, refresh_payload=refresh,
    )
    assert out == reports
    assert unresolved == []
    assert refresh_calls == []
    assert rerun_calls == []


# ----------------------------------------------------------------------
# Single request → dispatch + rerun → resolved
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_request_dispatches_and_reruns() -> None:
    req = _request("fundamentals_stale", "fundamentals")
    reports = [_report("fundamentals", requests=[req]), _report("news")]

    refresh_calls: list[tuple[str, str]] = []
    rerun_calls: list[str] = []

    async def refresh(kind: str, ticker: str) -> bool:
        refresh_calls.append((kind, ticker))
        return True

    async def rerun(role: str, _r: list[AgentReport]) -> AgentReport | None:
        rerun_calls.append(role)
        # Return a fresh report with NO remediation_requests — resolves it.
        return _report(role)

    out, unresolved = await apply_remediations_and_rerun(
        reports=reports, user_id="ariel", ticker="NOW", decision_run_id=1,
        rerun_analyst=rerun, refresh_payload=refresh,
    )
    assert refresh_calls == [("fundamentals_stale", "NOW")]
    assert rerun_calls == ["fundamentals"]
    assert unresolved == []
    # Fundamentals report was swapped — should no longer carry the request.
    fundamentals = [r for r in out if r.agent_role == "fundamentals"][0]
    assert fundamentals.output.remediation_requests == []


# ----------------------------------------------------------------------
# Refresh fails → request becomes unresolved, no rerun
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_failure_skips_rerun_and_surfaces_unresolved() -> None:
    req = _request("price_stale", "fundamentals")
    reports = [_report("fundamentals", requests=[req])]

    rerun_calls: list[str] = []

    async def refresh(_kind: str, _ticker: str) -> bool:
        return False  # refresh no-op / failure

    async def rerun(role: str, _r: list[AgentReport]) -> AgentReport | None:
        rerun_calls.append(role)
        return _report(role)

    out, unresolved = await apply_remediations_and_rerun(
        reports=reports, user_id="ariel", ticker="NOW", decision_run_id=1,
        rerun_analyst=rerun, refresh_payload=refresh,
    )
    assert rerun_calls == []  # no rerun because refresh failed
    assert len(unresolved) == 1
    assert unresolved[0].kind == "price_stale"
    assert out == reports  # unchanged


# ----------------------------------------------------------------------
# Persistent request → cap exhausted → unresolved surfaces
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cap_exhausted_surfaces_remaining_requests() -> None:
    req = _request("fundamentals_stale", "fundamentals")
    reports = [_report("fundamentals", requests=[req])]

    refresh_count = 0
    rerun_count = 0

    async def refresh(_kind: str, _ticker: str) -> bool:
        nonlocal refresh_count
        refresh_count += 1
        return True

    async def rerun(role: str, _r: list[AgentReport]) -> AgentReport | None:
        # Returns a NEW report that STILL carries the same request —
        # simulates a refresh that doesn't actually fix the underlying
        # data quality issue (e.g. yfinance itself is wrong).
        nonlocal rerun_count
        rerun_count += 1
        return _report(role, requests=[_request("fundamentals_stale", role)])

    out, unresolved = await apply_remediations_and_rerun(
        reports=reports, user_id="ariel", ticker="NOW", decision_run_id=1,
        rerun_analyst=rerun, refresh_payload=refresh,
    )
    # MAX_REMEDIATION_ROUNDS rounds attempted — each round refreshes +
    # reruns once. After cap, the request is still present.
    assert refresh_count == MAX_REMEDIATION_ROUNDS
    assert rerun_count == MAX_REMEDIATION_ROUNDS
    assert len(unresolved) == 1
    assert unresolved[0].kind == "fundamentals_stale"


# ----------------------------------------------------------------------
# Multiple requests from different analysts in one round
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_roles_each_refresh_and_rerun_once() -> None:
    req_fund = _request("fundamentals_stale", "fundamentals")
    req_news = _request("news_empty", "news")
    reports = [
        _report("fundamentals", requests=[req_fund]),
        _report("news", requests=[req_news]),
        _report("macro"),  # no requests; should be untouched
    ]

    refresh_calls: list[tuple[str, str]] = []
    rerun_calls: list[str] = []

    async def refresh(kind: str, ticker: str) -> bool:
        refresh_calls.append((kind, ticker))
        return True

    async def rerun(role: str, _r: list[AgentReport]) -> AgentReport | None:
        rerun_calls.append(role)
        return _report(role)  # fresh, no remediation requests

    out, unresolved = await apply_remediations_and_rerun(
        reports=reports, user_id="ariel", ticker="NOW", decision_run_id=1,
        rerun_analyst=rerun, refresh_payload=refresh,
    )
    assert set(refresh_calls) == {("fundamentals_stale", "NOW"), ("news_empty", "NOW")}
    assert set(rerun_calls) == {"fundamentals", "news"}
    assert unresolved == []
    # Macro report untouched.
    assert any(r.agent_role == "macro" for r in out)


# ----------------------------------------------------------------------
# Rerun returning None preserves the original report
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rerun_failure_preserves_original_report() -> None:
    req = _request("fundamentals_stale", "fundamentals")
    original = _report("fundamentals", requests=[req])
    reports = [original]

    async def refresh(_kind: str, _ticker: str) -> bool:
        return True

    async def rerun(_role: str, _r: list[AgentReport]) -> AgentReport | None:
        return None  # rerun failure

    out, unresolved = await apply_remediations_and_rerun(
        reports=reports, user_id="ariel", ticker="NOW", decision_run_id=1,
        rerun_analyst=rerun, refresh_payload=refresh,
    )
    # Original report stays — including its unresolved request.
    assert out == [original]
    assert len(unresolved) == 1
