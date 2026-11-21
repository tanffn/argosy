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
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from typing import Any, Callable

from argosy.logging import get_logger

_log = get_logger("argosy.fleet_reliability")


class FleetCallTimeout(RuntimeError):
    """The call overran its hard timeout and its claude.exe subtree was killed."""


class FleetCallUnavailable(RuntimeError):
    """The scope's circuit breaker is open — short-circuited without a call."""


@dataclass
class _ProviderAttempt:
    deadline: float
    scoped_cleanup: bool = False
    primary_timed_out: Callable[[], None] | None = None
    cancel_sdk_call: Callable[[], None] | None = None


_provider_attempt: ContextVar[_ProviderAttempt | None] = ContextVar("provider_attempt", default=None)


@contextmanager
def track_primary_timeout(on_timeout: Callable[[], None]):
    attempt = _provider_attempt.get()
    if attempt is None:
        yield
        return
    attempt.scoped_cleanup = True
    attempt.primary_timed_out = on_timeout
    try:
        yield
    finally:
        attempt.primary_timed_out = None


@asynccontextmanager
async def sdk_attempt_timeout(seconds: float):
    """Cancel this SDK stream on its own event loop, including Windows worker loops."""
    attempt = _provider_attempt.get()
    if attempt is not None:
        loop, task = asyncio.get_running_loop(), asyncio.current_task()
        attempt.scoped_cleanup = True
        seconds = remaining_sdk_seconds(seconds)
        attempt.cancel_sdk_call = lambda: loop.call_soon_threadsafe(task.cancel)
    try:
        async with asyncio.timeout(seconds):
            yield
    finally:
        if attempt is not None:
            attempt.cancel_sdk_call = None


def remaining_sdk_seconds(seconds: float) -> float:
    attempt = _provider_attempt.get()
    if attempt is not None:
        seconds = min(seconds, attempt.deadline - time.monotonic())
        if seconds <= 0:
            raise TimeoutError("Fleet attempt expired before the Claude SDK call")
    return seconds


def remaining_provider_seconds(timeout_seconds: float) -> float:
    """Give Codex only the remaining time in its owner's hard-limited attempt."""
    attempt = _provider_attempt.get()
    if attempt is None:
        return timeout_seconds
    remaining = attempt.deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Fleet attempt expired before Codex could start")
    attempt.scoped_cleanup = True
    return min(timeout_seconds, remaining)


def run_with_provider_deadline(
    invoke: Callable[[], Any], *, seconds: float, scope: str, killer: Callable[[], None],
) -> Any:
    """Keep the primary and its alternate within one worker's timeout."""
    attempt = _ProviderAttempt(time.monotonic() + seconds)
    ctx = copy_context()
    ctx.run(_provider_attempt.set, attempt)
    pool = _cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix=scope)
    try:
        future = pool.submit(ctx.run, invoke)
        try:
            return future.result(timeout=max(0.0, attempt.deadline - time.monotonic()))
        except _cf.TimeoutError as exc:
            # A completed worker can itself raise TimeoutError. That exception
            # belongs to the transport and requires no process-wide cleanup.
            if future.done() and future.exception() is exc:
                raise
            signal_timeout = attempt.primary_timed_out
            if signal_timeout:
                signal_timeout()
            cancel_call = attempt.cancel_sdk_call
            if cancel_call:
                try:
                    cancel_call()
                except RuntimeError:
                    pass  # the SDK worker finished and closed its loop
            elif not attempt.scoped_cleanup:
                killer()
            # The Codex transport owns its individual process tree and deadline.
            raise FleetCallTimeout(f"{scope}: exceeded {seconds:.0f}s hard timeout") from exc
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


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


#: Consult per-ticker analysts. No outer hard timeout — BaseAgent's own
#: asyncio.timeout(sdk_timeout_seconds) is the hang authority on the async path.
CONSULT_ANALYST_CONFIG = FleetRetryConfig()

#: Deploy decision-team reviewers (sync). Working reviewer calls run 15-40s;
#: 240s is generous headroom while still killing a genuine hang.
DEPLOY_REVIEWER_CONFIG = FleetRetryConfig(hard_timeout_s=240.0)

# One batch classification call over the unmapped held set.  It is automatic
# daily work, so a hung CLI must not stall the holdings-review cadence. One
# fresh retry spans a transient burst without turning a single classification
# job into an unbounded queue.
INSTRUMENT_CLASSIFIER_CONFIG = FleetRetryConfig(
    retries=1,
    hard_timeout_s=240.0,
)
INSTRUMENT_CLASSIFIER_FALLBACK_CONFIG = FleetRetryConfig(
    retries=0,
    hard_timeout_s=180.0,
)


# The exit-1 fingerprint, mirrored from BaseAgent's in-call detector: word-bounded
# so "exit code 137" never matches; the parenthesized form is exact-string.
_EXIT1_RE = re.compile(r"\bexit code 1\b")


def is_transient_fleet_error(exc: BaseException) -> bool:
    """True only for the KNOWN transient class: the claude.exe exit-1 flake
    (as surfaced through ``AgentRunError``'s message) or a timeout. Everything
    else is deterministic and must surface unretried."""
    if isinstance(exc, (FleetCallTimeout, asyncio.TimeoutError, TimeoutError)):
        return True
    text = str(exc)
    # Exit 1 is a process result, not a diagnosis. The inner SDK runner only
    # retries EMPTY-stderr exit-1 failures; do not undo that distinction here.
    if "[claude.exe stderr]" in text or any(marker in text.lower() for marker in (
        "remote managed settings", "authentication_error", "invalid api key",
        "not logged in", "unauthorized", "permission denied",
    )):
        return False
    if _EXIT1_RE.search(text) or "(exit code: 1)" in text:
        return True
    # BaseAgent preserves the SDK exception as its explicit cause. A timeout
    # has an empty string, so diagnosis must not depend on rendered prose.
    seen = {id(exc)}
    cause = exc.__cause__
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, (FleetCallTimeout, TimeoutError)):
            return True
        cause = cause.__cause__
    return False


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
    for attempt in range(cfg.retries + 1):
        try:
            result = await attempt_factory()
            breaker.record_success()
            return result
        except Exception as exc:  # noqa: BLE001 — classified below
            last_exc = exc
            if not is_transient_fleet_error(exc) or attempt >= cfg.retries:
                break
            delay = _backoff_delay(attempt, cfg)
            _log.warning(
                "fleet_reliability.transient_retry",
                scope=scope, attempt=attempt + 1, max_attempts=cfg.retries + 1,
                delay_s=delay, error=str(exc)[:200],
            )
            await sleep(delay)

    if is_transient_fleet_error(last_exc):
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
        return run_with_provider_deadline(
            attempt_factory, seconds=cfg.hard_timeout_s, scope=f"fleet-{scope}", killer=killer,
        )

    last_exc: BaseException | None = None
    for attempt in range(cfg.retries + 1):
        try:
            result = _one_attempt()
            breaker.record_success()
            return result
        except Exception as exc:  # noqa: BLE001 — classified below
            last_exc = exc
            if not is_transient_fleet_error(exc) or attempt >= cfg.retries:
                break
            delay = _backoff_delay(attempt, cfg)
            _log.warning(
                "fleet_reliability.transient_retry",
                scope=scope, attempt=attempt + 1, max_attempts=cfg.retries + 1,
                delay_s=delay, error=str(exc)[:200],
            )
            sleep(delay)

    if is_transient_fleet_error(last_exc):
        breaker.record_failure()
    assert last_exc is not None
    raise last_exc


__all__ = [
    "CONSULT_ANALYST_CONFIG",
    "DEPLOY_REVIEWER_CONFIG",
    "INSTRUMENT_CLASSIFIER_CONFIG",
    "INSTRUMENT_CLASSIFIER_FALLBACK_CONFIG",
    "CircuitBreaker",
    "FleetCallTimeout",
    "FleetCallUnavailable",
    "FleetRetryConfig",
    "call_reliably_async",
    "call_reliably_sync",
    "get_breaker",
    "is_transient_fleet_error",
    "_kill_claude_children",
]
