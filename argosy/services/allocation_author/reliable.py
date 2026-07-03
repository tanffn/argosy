"""Reliability wrapper for the deployment author — the P0 fix.

The fleet was unusable because the money decision depended on the bundled
``claude.exe`` CLI, which hangs (a live disposition call hung ~7 min and was
killed). A hang on the money path is the product being broken, not a nuisance. This
module makes the author path robust:

  * **Hard timeout + process-tree kill** — the author gets ~150s; on overrun we KILL
    the leaked ``claude.exe`` child processes of THIS process (never the user's own
    Claude Code session — only our subprocess subtree) instead of awaiting a zombie.
  * **Retry on a FRESH process** — one transient failure (timeout / exit-1) retries
    with a brand-new agent + subprocess, since process-state corruption doesn't
    survive a fresh spawn.
  * **Circuit breaker** — after repeated failures the breaker OPENS and short-circuits
    to the deterministic fallback for a cooldown, so a flaky CLI can't be hammered.
  * **Packet-hash cache** — an identical deploy request within the process returns the
    already-authored, already-verified proposal without a new LLM call.
  * **Backend selection** — routes to the ``api_key`` backend when configured (no
    subprocess at all), else the hardened ``claude_code`` path.

Honest degrade: on unavailability the outcome is ``unavailable`` (never a fabricated
allocation) and the caller falls back to the deterministic engine, LABELLED degraded.

The LLM call and the process kill are injectable (``run_author`` / ``killer``) so the
whole thing is unit-tested with no subprocess and no live model.
"""
from __future__ import annotations

import concurrent.futures as _cf
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from argosy.logging import get_logger
from argosy.services.allocation_author.flow import AuthorOutcome, run_allocation_author
from argosy.services.allocation_author.proposal import AllocationProposal
from argosy.services.allocation_author.verifier import GateReport, verify_allocation_proposal

_log = get_logger("argosy.allocation_author.reliable")


class AuthorTimeout(RuntimeError):
    """The author overran its hard timeout and its process tree was killed."""


@dataclass
class ReliabilityConfig:
    hard_timeout_s: float = 150.0        # per-attempt wall clock (not FM's 900s)
    retries: int = 1                     # extra attempts on transient failure (fresh process)
    breaker_fail_threshold: int = 3
    breaker_cooldown_s: float = 300.0
    backend: str | None = None           # None → config's deployment_author_backend / global


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
            _log.warning("deployment_author.breaker_open", failures=self.failures)


# Module-level breaker + cache persist across route calls in one process.
_BREAKER = CircuitBreaker()
_CACHE: dict[str, AllocationProposal] = {}


def packet_hash(packet: dict[str, Any]) -> str:
    """Stable hash of the decision-relevant packet fields (sets normalised to
    sorted lists so element order never changes the key)."""
    def _norm(v: Any) -> Any:
        if isinstance(v, set):
            return sorted(str(x) for x in v)
        if isinstance(v, dict):
            return {k: _norm(v[k]) for k in sorted(v)}
        if isinstance(v, (list, tuple)):
            return [_norm(x) for x in v]
        return v

    blob = json.dumps(_norm(packet), sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _kill_claude_children() -> None:
    """Kill the ``claude.exe`` subprocesses spawned under THIS process. Scoped to
    our own subtree via psutil — it never touches sibling / parent claude.exe (e.g.
    the developer's Claude Code session), only the children the SDK spawned here."""
    try:
        import os

        import psutil
    except Exception as exc:  # noqa: BLE001 — psutil missing: nothing we can do safely
        _log.warning("deployment_author.kill_unavailable", error=str(exc)[:120])
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
            _log.warning("deployment_author.killed_children", n=len(victims))
    except Exception as exc:  # noqa: BLE001 — kill is best-effort, never raises
        _log.warning("deployment_author.kill_failed", error=str(exc)[:120])


def _invoke_agent(agent: Any, packet: dict[str, Any], feedback: list | None) -> AllocationProposal:
    report = agent.run_sync(packet=packet, feedback=feedback)
    return report.output


def _run_author_with_timeout(
    agent_factory: Callable[[], Any],
    packet: dict[str, Any],
    feedback: list | None,
    *,
    hard_timeout_s: float,
    killer: Callable[[], None] = _kill_claude_children,
) -> AllocationProposal:
    """Run one author attempt with an authoritative hard timeout. On overrun, kill
    the leaked claude.exe subtree and raise ``AuthorTimeout`` (the SDK's own soft
    timeout is a backstop, not the authority — a hang must not await minutes)."""
    agent = agent_factory()
    ex = _cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="deploy-author")
    fut = ex.submit(_invoke_agent, agent, packet, feedback)
    try:
        proposal = fut.result(timeout=hard_timeout_s)
        ex.shutdown(wait=False)
        return proposal
    except _cf.TimeoutError as exc:
        # Kill the subprocess first so the abandoned worker thread unblocks and the
        # SDK call errors out instead of lingering; then don't wait on it.
        killer()
        ex.shutdown(wait=False)
        raise AuthorTimeout(
            f"deployment author exceeded {hard_timeout_s:.0f}s hard timeout"
        ) from exc


def _default_agent_factory(user_id: str, backend: str | None) -> Callable[[], Any]:
    def make() -> Any:
        from argosy.agents.deployment_author import DeploymentAuthorAgent

        agent = DeploymentAuthorAgent(user_id=user_id)
        if backend:
            agent._backend_override = backend  # honoured in BaseAgent._call_model
        return agent

    return make


def authored_allocation(
    packet: dict[str, Any],
    *,
    user_id: str,
    config: ReliabilityConfig | None = None,
    agent_factory: Callable[[], Any] | None = None,
    run_author: Callable[..., AllocationProposal] | None = None,
    breaker: CircuitBreaker | None = None,
    cache: dict[str, AllocationProposal] | None = None,
    verify: Callable[..., GateReport] = verify_allocation_proposal,
    max_revisions: int = 2,
) -> AuthorOutcome:
    """Author→verify→bounce with the full reliability envelope. Returns an
    ``AuthorOutcome`` (accepted / rejected / unavailable); the caller degrades to the
    deterministic engine on the latter two. Never fabricates an allocation."""
    cfg = config or ReliabilityConfig()
    breaker = breaker if breaker is not None else _BREAKER
    cache = cache if cache is not None else _CACHE
    run_author = run_author or _run_author_with_timeout

    # Resolve the money-path backend (config override → global default in the agent).
    resolved_backend = cfg.backend
    if resolved_backend is None:
        try:
            from argosy.config import get_settings
            resolved_backend = get_settings().deployment_author_backend
        except Exception:  # noqa: BLE001
            resolved_backend = None
    factory = agent_factory or _default_agent_factory(user_id, resolved_backend)

    key = packet_hash(packet)
    cached = cache.get(key)
    if cached is not None:
        _log.info("deployment_author.cache_hit")
        return AuthorOutcome(status="accepted", proposal=cached,
                             report=verify(cached, packet), attempts=0)

    def reliable_author_fn(pkt: dict[str, Any], feedback: list | None) -> AllocationProposal | None:
        if not breaker.allow():
            _log.warning("deployment_author.circuit_open_short_circuit")
            return None  # → flow: unavailable → degraded fallback
        last_exc: Exception | None = None
        for attempt in range(cfg.retries + 1):
            try:
                proposal = run_author(
                    factory, pkt, feedback, hard_timeout_s=cfg.hard_timeout_s
                )
                breaker.record_success()
                return proposal
            except Exception as exc:  # noqa: BLE001 — transient CLI failure; retry fresh
                last_exc = exc
                _log.warning(
                    "deployment_author.attempt_failed",
                    attempt=attempt + 1, error=str(exc)[:200],
                )
        # Exhausted all attempts for this call — one breaker failure, then raise
        # so the flow marks the author unavailable.
        breaker.record_failure()
        raise last_exc if last_exc else RuntimeError("author failed")

    outcome = run_allocation_author(
        packet, author_fn=reliable_author_fn, max_revisions=max_revisions, verify=verify,
    )
    if outcome.status == "accepted" and outcome.proposal is not None:
        cache[key] = outcome.proposal
    return outcome


__all__ = [
    "AuthorTimeout",
    "CircuitBreaker",
    "ReliabilityConfig",
    "authored_allocation",
    "packet_hash",
    "_run_author_with_timeout",
]
