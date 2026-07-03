"""Reliability wrapper for the deployment author — the P0 fix that made the flaky
claude.exe fleet path usable: hard timeout + process-tree kill + circuit breaker +
packet-hash cache + honest degrade. Tested with injected fakes (no LLM, no subprocess)."""
from __future__ import annotations

import pytest

from argosy.services.allocation_author.proposal import AllocationProposal, Buy
from argosy.services.allocation_author.reliable import (
    AuthorTimeout,
    CircuitBreaker,
    ReliabilityConfig,
    _run_author_with_timeout,
    authored_allocation,
)

_PACKET = {
    "deployable_usd": 180_000.0,
    "holdings": {"SCHD": 264_000.0}, "known_symbols": {"EXUS", "SPMV"},
    "plan_menu": [], "nvda": {"pct": 60.0, "cap_pct": 30.0},
    "reserve": {"shortfall_usd": 0.0}, "instrument_facts": [],
    "policy_signals": {}, "user_constraints": "",
}


def _good():
    return AllocationProposal(
        cash_to_deploy=180_000.0,
        buys=[Buy(symbol="EXUS", amount_usd=120_000.0, sleeve="ex-US", claimed_us_weight=0.0),
              Buy(symbol="SPMV", amount_usd=60_000.0, sleeve="US low-vol", claimed_us_weight=1.0)],
        rationale="fill the ex-US and low-vol gaps; no US-large-cap into a NVDA-heavy book",
    )


# --- happy path + caching ------------------------------------------------
def test_accepts_and_caches_by_packet_hash():
    calls = {"n": 0}

    def run_author(agent_factory, packet, feedback, *, hard_timeout_s):
        calls["n"] += 1
        return _good()

    cache: dict = {}
    out1 = authored_allocation(_PACKET, user_id="ariel", run_author=run_author,
                               cache=cache, breaker=CircuitBreaker())
    assert out1.status == "accepted"
    assert calls["n"] == 1
    # Second identical packet → served from cache, no new author call.
    out2 = authored_allocation(_PACKET, user_id="ariel", run_author=run_author,
                               cache=cache, breaker=CircuitBreaker())
    assert out2.status == "accepted"
    assert calls["n"] == 1


def test_packet_hash_stable_across_set_ordering():
    from argosy.services.allocation_author.reliable import packet_hash
    a = dict(_PACKET, known_symbols={"EXUS", "SPMV"})
    b = dict(_PACKET, known_symbols={"SPMV", "EXUS"})
    assert packet_hash(a) == packet_hash(b)


# --- retry on a fresh process -------------------------------------------
def test_retries_transient_failure_on_fresh_process():
    calls = {"run": 0, "factory": 0}

    def agent_factory():
        calls["factory"] += 1
        return object()

    def run_author(agent_factory, packet, feedback, *, hard_timeout_s):
        calls["run"] += 1
        agent_factory()  # a fresh agent each attempt
        if calls["run"] == 1:
            raise AuthorTimeout("claude.exe hung")
        return _good()

    out = authored_allocation(
        _PACKET, user_id="ariel", run_author=run_author,
        agent_factory=agent_factory, breaker=CircuitBreaker(),
        config=ReliabilityConfig(retries=1),
    )
    assert out.status == "accepted"
    assert calls["run"] == 2 and calls["factory"] == 2  # fresh process on retry


def test_exhausted_retries_degrade_to_unavailable():
    def run_author(agent_factory, packet, feedback, *, hard_timeout_s):
        raise AuthorTimeout("still hung")

    out = authored_allocation(
        _PACKET, user_id="ariel", run_author=run_author,
        breaker=CircuitBreaker(), config=ReliabilityConfig(retries=1), cache={},
    )
    assert out.status == "unavailable"


# --- circuit breaker -----------------------------------------------------
def test_breaker_opens_after_threshold_and_short_circuits():
    t = {"now": 0.0}
    br = CircuitBreaker(fail_threshold=2, cooldown_s=300.0, clock=lambda: t["now"])
    ran = {"n": 0}

    def run_author(agent_factory, packet, feedback, *, hard_timeout_s):
        ran["n"] += 1
        raise AuthorTimeout("hung")

    # Each failing deploy records one breaker failure (retries=0 here).
    cfg = ReliabilityConfig(retries=0)
    authored_allocation(_PACKET, user_id="ariel", run_author=run_author, breaker=br, config=cfg, cache={})
    authored_allocation(_PACKET, user_id="ariel", run_author=run_author, breaker=br, config=cfg, cache={})
    assert ran["n"] == 2
    # Breaker now OPEN → next call short-circuits without invoking run_author.
    out = authored_allocation(_PACKET, user_id="ariel", run_author=run_author, breaker=br, config=cfg, cache={})
    assert out.status == "unavailable"
    assert ran["n"] == 2  # not called while open


def test_breaker_half_opens_after_cooldown():
    t = {"now": 0.0}
    br = CircuitBreaker(fail_threshold=1, cooldown_s=300.0, clock=lambda: t["now"])
    seq = {"n": 0}

    def run_author(agent_factory, packet, feedback, *, hard_timeout_s):
        seq["n"] += 1
        if seq["n"] == 1:
            raise AuthorTimeout("hung")
        return _good()

    cfg = ReliabilityConfig(retries=0)
    o1 = authored_allocation(_PACKET, user_id="ariel", run_author=run_author, breaker=br, config=cfg, cache={})
    assert o1.status == "unavailable"
    # Open now. Before cooldown → short-circuit.
    o2 = authored_allocation(_PACKET, user_id="ariel", run_author=run_author, breaker=br, config=cfg, cache={})
    assert o2.status == "unavailable" and seq["n"] == 1
    # Advance past cooldown → half-open, allows a trial that now succeeds.
    t["now"] = 301.0
    o3 = authored_allocation(_PACKET, user_id="ariel", run_author=run_author, breaker=br, config=cfg, cache={})
    assert o3.status == "accepted" and seq["n"] == 2


# --- hard timeout + process kill mechanics -------------------------------
def test_run_author_with_timeout_kills_on_hang():
    import time

    killed = {"n": 0}

    class _SlowAgent:
        def run_sync(self, **kw):
            time.sleep(5.0)  # simulate a hung claude.exe
            raise AssertionError("should have been killed")

    with pytest.raises(AuthorTimeout):
        _run_author_with_timeout(
            lambda: _SlowAgent(), _PACKET, None,
            hard_timeout_s=0.2, killer=lambda: killed.__setitem__("n", killed["n"] + 1),
        )
    assert killed["n"] == 1  # the process-tree killer fired on timeout


def test_run_author_with_timeout_returns_output_on_success():
    class _FastAgent:
        def run_sync(self, **kw):
            class _R:
                output = _good()
            return _R()

    out = _run_author_with_timeout(
        lambda: _FastAgent(), _PACKET, None, hard_timeout_s=5.0, killer=lambda: None,
    )
    assert out.cash_to_deploy == 180_000.0
