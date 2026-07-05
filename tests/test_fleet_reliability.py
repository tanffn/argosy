"""Shared fleet reliability runner: known-transient-only retries with long
backoff, per-scope breaker, hard timeout + kill (sync), and the consult-analyst
/ deploy-reviewer wirings. No live LLM, no real sleeps."""
from __future__ import annotations

import asyncio

import pytest

from argosy.agents.errors import AgentRunError
from argosy.services.fleet_reliability import (
    CircuitBreaker,
    FleetCallTimeout,
    FleetCallUnavailable,
    FleetRetryConfig,
    call_reliably_async,
    call_reliably_sync,
    get_breaker,
    is_transient_fleet_error,
)

EXIT1_MSG = (
    "fundamentals: claude-agent-sdk error: Command failed with exit code 1 "
    "(exit code: 1)\nError output: Check stderr output for details\n"
    "[claude.exe stderr was empty]"
)


# ----------------------------------------------------------------------
# Transient classification — retry ONLY the known-transient fingerprint.
# ----------------------------------------------------------------------


def test_exit1_fingerprint_is_transient():
    assert is_transient_fleet_error(AgentRunError(EXIT1_MSG))


def test_exit_code_137_is_not_transient():
    assert not is_transient_fleet_error(AgentRunError("Command failed with exit code 137"))


def test_timeouts_are_transient():
    assert is_transient_fleet_error(FleetCallTimeout("scope: exceeded 240s"))
    assert is_transient_fleet_error(asyncio.TimeoutError())


def test_deterministic_errors_are_not_transient():
    assert not is_transient_fleet_error(ValueError("bad schema"))
    assert not is_transient_fleet_error(AgentRunError("model returned 400 invalid_request"))


# ----------------------------------------------------------------------
# Async runner — the consult-analyst path.
# ----------------------------------------------------------------------


def _cfg(**kw):
    return FleetRetryConfig(**{"retries": 2, "backoff_base_s": 20.0, **kw})


def test_async_retries_transient_then_succeeds():
    calls, delays = [], []

    async def attempt():
        calls.append(1)
        if len(calls) < 3:
            raise AgentRunError(EXIT1_MSG)
        return "report"

    async def fake_sleep(d):
        delays.append(d)

    breaker = CircuitBreaker()
    result = asyncio.run(call_reliably_async(
        attempt, scope="t", config=_cfg(), breaker=breaker, sleep=fake_sleep,
    ))
    assert result == "report"
    assert len(calls) == 3
    # LONG backoff — spans the burst (20s, then 40s), not sub-second.
    assert delays == [20.0, 40.0]
    assert breaker.failures == 0


def test_async_deterministic_error_is_never_retried():
    calls = []

    async def attempt():
        calls.append(1)
        raise ValueError("schema mismatch")

    async def fail_sleep(d):  # pragma: no cover - must not be reached
        raise AssertionError("slept on a deterministic error")

    breaker = CircuitBreaker()
    with pytest.raises(ValueError):
        asyncio.run(call_reliably_async(
            attempt, scope="t", config=_cfg(), breaker=breaker, sleep=fail_sleep,
        ))
    assert len(calls) == 1
    assert breaker.failures == 0  # deterministic errors don't count against the CLI


def test_async_exhausted_raises_last_error_and_counts_breaker_failure():
    async def attempt():
        raise AgentRunError(EXIT1_MSG)

    async def fake_sleep(d):
        pass

    breaker = CircuitBreaker(fail_threshold=3)
    with pytest.raises(AgentRunError):
        asyncio.run(call_reliably_async(
            attempt, scope="t", config=_cfg(), breaker=breaker, sleep=fake_sleep,
        ))
    assert breaker.failures == 1


def test_async_open_breaker_short_circuits_without_calling():
    calls = []

    async def attempt():  # pragma: no cover - must not be reached
        calls.append(1)

    breaker = CircuitBreaker(fail_threshold=1)
    breaker.record_failure()  # opens
    with pytest.raises(FleetCallUnavailable):
        asyncio.run(call_reliably_async(attempt, scope="t", config=_cfg(), breaker=breaker))
    assert calls == []


# ----------------------------------------------------------------------
# Sync runner — the deploy-reviewer path (hard timeout + kill).
# ----------------------------------------------------------------------


def test_sync_retries_transient_then_succeeds():
    calls, delays = [], []

    def attempt():
        calls.append(1)
        if len(calls) < 2:
            raise AgentRunError(EXIT1_MSG)
        return "review"

    result = call_reliably_sync(
        attempt, scope="t", config=_cfg(), breaker=CircuitBreaker(),
        sleep=delays.append,
    )
    assert result == "review"
    assert delays == [20.0]


def test_sync_hard_timeout_kills_and_retries():
    kills, delays, calls = [], [], []

    def attempt():
        calls.append(1)
        if len(calls) == 1:
            import time as _t
            _t.sleep(5)  # exceeds the 0.2s hard timeout below
        return "review"

    result = call_reliably_sync(
        attempt, scope="t",
        config=FleetRetryConfig(retries=1, backoff_base_s=1.0, hard_timeout_s=0.2),
        breaker=CircuitBreaker(),
        sleep=delays.append,
        killer=lambda: kills.append(1),
    )
    assert result == "review"
    assert kills == [1]       # the hung attempt's subtree was killed
    assert len(calls) == 2


def test_sync_open_breaker_short_circuits():
    breaker = CircuitBreaker(fail_threshold=1)
    breaker.record_failure()
    with pytest.raises(FleetCallUnavailable):
        call_reliably_sync(lambda: "x", scope="t", config=_cfg(), breaker=breaker)


def test_get_breaker_is_scoped_and_cached():
    a1 = get_breaker("scope_a_unique_test")
    a2 = get_breaker("scope_a_unique_test")
    b = get_breaker("scope_b_unique_test")
    assert a1 is a2 and a1 is not b


# ----------------------------------------------------------------------
# Wiring — the consult analysts + deploy reviewers actually go through it.
# ----------------------------------------------------------------------


def test_consult_analyst_runner_retries_exit1(monkeypatch):
    """A macro analyst that dies once on the exit-1 flake succeeds on the
    outer retry — the exact live failure on decision_runs 122/123."""
    import argosy.decisions.per_ticker_analysts as pta
    from argosy.agents.macro_analyst import MacroAnalystAgent
    from argosy.services import fleet_reliability as fr

    attempts = []

    async def flaky_run(self, **inputs):
        attempts.append(1)
        if len(attempts) == 1:
            raise AgentRunError(EXIT1_MSG)
        return "macro-report"

    monkeypatch.setattr(MacroAnalystAgent, "__init__", lambda self, user_id: None)
    monkeypatch.setattr(MacroAnalystAgent, "run", flaky_run)
    # No real 20s sleeps in tests.
    monkeypatch.setattr(
        fr, "CONSULT_ANALYST_CONFIG", fr.FleetRetryConfig(retries=2, backoff_base_s=0.0),
    )
    fr._BREAKERS.pop("consult_analysts", None)

    report = asyncio.run(pta._run_macro("ariel", {"cpi": 3.1}))
    assert report == "macro-report"
    assert len(attempts) == 2


def test_deploy_reviewer_default_review_retries_exit1(monkeypatch):
    from argosy.agents.deployment_reviewer import (
        DeploymentReviewerAgent,
        DeploymentReviewOutput,
    )
    from argosy.services import fleet_reliability as fr
    from argosy.services.deploy_decision_team import _default_review

    attempts = []

    def flaky_run_sync(self, **inputs):
        attempts.append(1)
        if len(attempts) == 1:
            raise AgentRunError(EXIT1_MSG)

        class _Report:
            output = DeploymentReviewOutput(lens="prudence", objections=[])

        return _Report()

    monkeypatch.setattr(DeploymentReviewerAgent, "__init__", lambda self, user_id: None)
    monkeypatch.setattr(DeploymentReviewerAgent, "run_sync", flaky_run_sync)
    monkeypatch.setattr(
        fr, "DEPLOY_REVIEWER_CONFIG",
        fr.FleetRetryConfig(retries=2, backoff_base_s=0.0, hard_timeout_s=None),
    )
    fr._BREAKERS.pop("deploy_reviewers", None)

    out = _default_review("prudence", {}, [], user_id="ariel")
    assert out.lens == "prudence"
    assert len(attempts) == 2
