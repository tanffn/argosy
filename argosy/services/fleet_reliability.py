"""Shared fleet reliability runner — the P0 wrapper, generalized to every agent.

Live signature (2026-07-05, decision_runs 122/123 + app logs): the claude.exe
exit-1 flake arrives in BURSTS lasting minutes (peaks of 65-92 retries/hour),
during which nearly every fresh spawn fails. ``BaseAgent``'s in-call retries
(0.5/1/2s backoff, budget 3) burn out inside ~40s — well inside a burst — so
every consult analyst exhausted them and the ≥2 quorum failed twice.

This module adds the missing OUTER layer, shared by the whole fleet:

  * **Fresh-everything retries with LONG backoff** — a failed call is retried on a
    brand-new agent object + subprocess after tens of seconds (default 20s, 40s),
    so the retry lands past the burst instead of inside it.
  * **Known-transient only** — retry fires ONLY on the exit-1 fingerprint or a
    timeout. A deterministic failure (schema, citations, bad input, model 400)
    surfaces immediately; the wrapper never masks real errors.
  * **Per-scope circuit breaker** — repeated exhausted calls open the breaker and
    short-circuit with ``FleetCallUnavailable`` for a cooldown, so a long outage
    is not hammered. Callers keep their existing degrade path (skipped analyst,
    fail-open reviewer) — the wrapper never fabricates output.
  * **Hard timeout + process-tree kill (sync path)** — a hung call is killed
    (only claude.exe children of THIS process) instead of awaited forever.

The deployment author's ``allocation_author/reliable.py`` predates this module
and keeps its own flow-specific envelope (packet cache, verify/bounce); it
imports the breaker + killer from here so the primitives live in one place.
"""
from __future__ import annotations

import asyncio
import concurrent.futures as _cf
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from argosy.logging import get_logger

_log = get_logger("argosy.fleet_reliability")


class FleetCallTimeout(RuntimeError):
    """The call overran its hard timeout and its claude.exe subtree was killed."""


class FleetCallUnavailable(RuntimeError):
    """The scope's circuit breaker is open — short-circuited without a call."""


class FleetStructuralRetryError(RuntimeError):
    """Mechanical pipeline integrity miss — retry with a fresh agent, then degrade.

    Distinct from judgment failures (another agent re-derives those) and from
    transient flakes (exit-1 / timeout). Example: the bear produced zero
    ``tool_retrieved_urls`` despite independent retrieval being mandatory.
    Requested behaviour is not guaranteed behaviour; this error forces a
    recovery attempt through ``call_reliably_*`` rather than silently
    continuing with ``shared_payload``-only debate as actionable.
    """


class CircuitBreaker:
    """Trip after ``fail_threshold`` consecutive failures; short-circuit for
    ``cooldown_s`` then half-open (allow one trial, reset on its success)."""

    def __init__(
        self,
        *,
        fail_threshold: int = 3,
        cooldown_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.fail_threshold = fail_threshold
        self.cooldown_s = cooldown_s
        self._clock = clock
        self.failures = 0
        self.opened_at: float | None = None

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        if self._clock() - self.opened_at >= self.cooldown_s:
            # Half-open: reset and allow a single trial call.
            self.opened_at = None
            self.failures = 0
            return True
        return False

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.fail_threshold:
            self.opened_at = self._clock()
            _log.warning("fleet_reliability.breaker_open", failures=self.failures)


#: One breaker per scope ("consult_analysts", "deploy_reviewers", ...), so an
#: outage in one call family can't silently disable another.
_BREAKERS: dict[str, CircuitBreaker] = {}


def get_breaker(scope: str, *, fail_threshold: int = 3, cooldown_s: float = 300.0) -> CircuitBreaker:
    breaker = _BREAKERS.get(scope)
    if breaker is None:
        breaker = CircuitBreaker(fail_threshold=fail_threshold, cooldown_s=cooldown_s)
        _BREAKERS[scope] = breaker
    return breaker


@dataclass
class FleetRetryConfig:
    """Outer-envelope knobs. Backoff is LONG on purpose — the transient we are
    spanning is a minutes-long burst, not a per-call blip (the per-call blips
    are already retried inside ``BaseAgent`` with sub-second backoff)."""

    retries: int = 2                     # extra attempts beyond the first
    backoff_base_s: float = 20.0         # 20s, then 40s
    backoff_cap_s: float = 60.0
    hard_timeout_s: float | None = None  # sync path per-attempt wall clock; None = no outer cap
    # Total wall-clock across ALL attempts + backoffs. When exceeded, stop
    # retrying and raise the last error (prevents stacked inner+outer burns).
    total_wall_clock_s: float | None = None


#: Consult per-ticker analysts. No outer hard timeout — BaseAgent's own
#: asyncio.timeout(sdk_timeout_seconds) is the hang authority on the async path.
CONSULT_ANALYST_CONFIG = FleetRetryConfig()

#: Deploy decision-team reviewers (sync). Working reviewer calls run 15-40s;
#: 240s is generous headroom while still killing a genuine hang.
DEPLOY_REVIEWER_CONFIG = FleetRetryConfig(hard_timeout_s=240.0)

#: Pre-debate premise check (async). Outer long-backoff envelope; hang
#: authority remains BaseAgent's per-role sdk_timeout (90s for premise_check).
#: retries=1 + inner max_retries=0 + total_wall_clock_s=240 → ≤ ~4 min.
#: Failure degrades to explicit unverified, not abort.
PREMISE_CHECK_CONFIG = FleetRetryConfig(
    retries=1, backoff_base_s=20.0, total_wall_clock_s=240.0,
)

#: Bear independent-retrieval enforcement. Structural miss (zero tool URLs)
#: is retryable once with short backoff, then loud green_light block.
#: Not a judgment gate — "did WebSearch run?" is a mechanical fact.
#: Short backoff: this is a fresh-agent re-ask, not an exit-1 burst span.
BEAR_INDEPENDENCE_CONFIG = FleetRetryConfig(
    retries=1, backoff_base_s=0.5, backoff_cap_s=2.0, total_wall_clock_s=120.0,
)


# The exit-1 fingerprint, mirrored from BaseAgent's in-call detector: word-bounded
# so "exit code 137" never matches; the parenthesized form is exact-string.
_EXIT1_RE = re.compile(r"\bexit code 1\b")


def is_transient_fleet_error(exc: BaseException) -> bool:
    """True only for the KNOWN transient class: the claude.exe exit-1 flake
    (as surfaced through ``AgentRunError``'s message), a timeout, or an
    ``AgentRunError`` whose ``__cause__`` is a timeout. Everything else is
    deterministic and must surface unretried."""
    if isinstance(exc, (FleetCallTimeout, asyncio.TimeoutError, TimeoutError)):
        return True
    # Walk the cause chain — BaseAgent wraps asyncio.TimeoutError in
    # AgentRunError; without this the outer reliability wrapper never
    # retries the failure mode it was added for.
    cause: BaseException | None = exc
    seen: set[int] = set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, (asyncio.TimeoutError, TimeoutError, FleetCallTimeout)):
            return True
        cause = cause.__cause__ or cause.__context__
    text = str(exc).lower()
    if "timeout" in text or "timed out" in text:
        return True
    text_raw = str(exc)
    return bool(_EXIT1_RE.search(text_raw) or "(exit code: 1)" in text_raw)


def is_retryable_fleet_error(exc: BaseException) -> bool:
    """True for transient flakes OR mechanical integrity misses that warrant retry.

    ``FleetStructuralRetryError`` is retryable then must degrade loudly —
    it is not a judgment failure and must not be silently swallowed.
    """
    if isinstance(exc, FleetStructuralRetryError):
        return True
    return is_transient_fleet_error(exc)


def _backoff_delay(attempt: int, config: FleetRetryConfig) -> float:
    return min(config.backoff_cap_s, config.backoff_base_s * (2 ** attempt))


def _kill_claude_children() -> None:
    """Kill the ``claude.exe`` subprocesses spawned under THIS process. Scoped to
    our own subtree via psutil — it never touches sibling / parent claude.exe (e.g.
    the developer's Claude Code session), only the children the SDK spawned here."""
    try:
        import os

        import psutil
    except Exception as exc:  # noqa: BLE001 — psutil missing: nothing we can do safely
        _log.warning("fleet_reliability.kill_unavailable", error=str(exc)[:120])
        return
    try:
        me = psutil.Process(os.getpid())
        victims = [
            c for c in me.children(recursive=True)
            if "claude" in (c.name() or "").lower()
        ]
        for c in victims:
            try:
                c.kill()
            except Exception:  # noqa: BLE001 — best-effort per child
                continue
        if victims:
            psutil.wait_procs(victims, timeout=3)
            _log.warning("fleet_reliability.killed_children", n=len(victims))
    except Exception as exc:  # noqa: BLE001 — kill is best-effort, never raises
        _log.warning("fleet_reliability.kill_failed", error=str(exc)[:120])


async def call_reliably_async(
    attempt_factory: Callable[[], Any],
    *,
    scope: str,
    config: FleetRetryConfig | None = None,
    breaker: CircuitBreaker | None = None,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> Any:
    """Run ``await attempt_factory()`` with the outer reliability envelope.

    ``attempt_factory`` must build everything fresh (agent object included) so a
    retry shares no state with the failed attempt. Raises the last error when
    retries are exhausted, or ``FleetCallUnavailable`` when the breaker is open.
    """
    cfg = config or FleetRetryConfig()
    breaker = breaker if breaker is not None else get_breaker(scope)
    if not breaker.allow():
        _log.warning("fleet_reliability.circuit_open", scope=scope)
        raise FleetCallUnavailable(f"{scope}: circuit breaker open")

    last_exc: BaseException | None = None
    started = time.monotonic()
    for attempt in range(cfg.retries + 1):
        try:
            result = await attempt_factory()
            breaker.record_success()
            return result
        except Exception as exc:  # noqa: BLE001 — classified below
            last_exc = exc
            if not is_retryable_fleet_error(exc) or attempt >= cfg.retries:
                break
            if (
                cfg.total_wall_clock_s is not None
                and (time.monotonic() - started) >= cfg.total_wall_clock_s
            ):
                _log.warning(
                    "fleet_reliability.total_wall_clock_exhausted",
                    scope=scope,
                    elapsed_s=round(time.monotonic() - started, 1),
                    budget_s=cfg.total_wall_clock_s,
                )
                break
            delay = _backoff_delay(attempt, cfg)
            remaining = None
            if cfg.total_wall_clock_s is not None:
                remaining = cfg.total_wall_clock_s - (time.monotonic() - started)
                if remaining <= 0:
                    break
                delay = min(delay, remaining)
            _log.warning(
                "fleet_reliability.transient_retry",
                scope=scope, attempt=attempt + 1, max_attempts=cfg.retries + 1,
                delay_s=delay, error=str(exc)[:200],
            )
            await sleep(delay)

    if is_retryable_fleet_error(last_exc):
        breaker.record_failure()
    assert last_exc is not None
    raise last_exc


def call_reliably_sync(
    attempt_factory: Callable[[], Any],
    *,
    scope: str,
    config: FleetRetryConfig | None = None,
    breaker: CircuitBreaker | None = None,
    sleep: Callable[[float], None] = time.sleep,
    killer: Callable[[], None] = _kill_claude_children,
) -> Any:
    """Sync sibling of ``call_reliably_async`` with a hard per-attempt timeout:
    on overrun the claude.exe subtree is killed and the attempt counts as
    transient (``FleetCallTimeout``)."""
    cfg = config or FleetRetryConfig()
    breaker = breaker if breaker is not None else get_breaker(scope)
    if not breaker.allow():
        _log.warning("fleet_reliability.circuit_open", scope=scope)
        raise FleetCallUnavailable(f"{scope}: circuit breaker open")

    def _one_attempt() -> Any:
        if cfg.hard_timeout_s is None:
            return attempt_factory()
        ex = _cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"fleet-{scope}")
        fut = ex.submit(attempt_factory)
        try:
            result = fut.result(timeout=cfg.hard_timeout_s)
            ex.shutdown(wait=False)
            return result
        except _cf.TimeoutError as exc:
            # Kill the subprocess first so the abandoned worker thread unblocks;
            # then don't wait on it.
            killer()
            ex.shutdown(wait=False)
            raise FleetCallTimeout(
                f"{scope}: exceeded {cfg.hard_timeout_s:.0f}s hard timeout"
            ) from exc

    last_exc: BaseException | None = None
    started = time.monotonic()
    for attempt in range(cfg.retries + 1):
        try:
            result = _one_attempt()
            breaker.record_success()
            return result
        except Exception as exc:  # noqa: BLE001 — classified below
            last_exc = exc
            if not is_retryable_fleet_error(exc) or attempt >= cfg.retries:
                break
            if (
                cfg.total_wall_clock_s is not None
                and (time.monotonic() - started) >= cfg.total_wall_clock_s
            ):
                _log.warning(
                    "fleet_reliability.total_wall_clock_exhausted",
                    scope=scope,
                    elapsed_s=round(time.monotonic() - started, 1),
                    budget_s=cfg.total_wall_clock_s,
                )
                break
            delay = _backoff_delay(attempt, cfg)
            if cfg.total_wall_clock_s is not None:
                remaining = cfg.total_wall_clock_s - (time.monotonic() - started)
                if remaining <= 0:
                    break
                delay = min(delay, remaining)
            _log.warning(
                "fleet_reliability.transient_retry",
                scope=scope, attempt=attempt + 1, max_attempts=cfg.retries + 1,
                delay_s=delay, error=str(exc)[:200],
            )
            sleep(delay)

    if is_retryable_fleet_error(last_exc):
        breaker.record_failure()
    assert last_exc is not None
    raise last_exc


__all__ = [
    "BEAR_INDEPENDENCE_CONFIG",
    "CONSULT_ANALYST_CONFIG",
    "DEPLOY_REVIEWER_CONFIG",
    "PREMISE_CHECK_CONFIG",
    "CircuitBreaker",
    "FleetCallTimeout",
    "FleetCallUnavailable",
    "FleetRetryConfig",
    "FleetStructuralRetryError",
    "call_reliably_async",
    "call_reliably_sync",
    "get_breaker",
    "is_retryable_fleet_error",
    "is_transient_fleet_error",
    "_kill_claude_children",
]
