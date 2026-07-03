"""author → verify → bounce orchestration — the control flow of the inversion.

The fleet AUTHORS an ``AllocationProposal``; the deterministic verifier gates it. On a
fixable failure the exact machine-readable failures are bounced back to the SAME author
(up to ``max_revisions``). Outcomes:

  * ``accepted``    — a proposal passed the gate; use it.
  * ``rejected``    — the author couldn't produce a passing proposal within the retries;
                      the caller falls back to the deterministic engine (labelled degraded).
  * ``unavailable`` — the author errored / returned nothing (e.g. claude.exe timeout);
                      the caller falls back to the deterministic engine (labelled degraded).

``author_fn(packet, feedback)`` returns an ``AllocationProposal`` (or None / raises when
unavailable). ``feedback`` is the prior gate's failures (None on the first attempt).
Injectable ``author_fn`` / ``verify`` so the whole loop is testable without a live LLM.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from argosy.logging import get_logger
from argosy.services.allocation_author.proposal import AllocationProposal
from argosy.services.allocation_author.verifier import (
    GateReport,
    GateStatus,
    verify_allocation_proposal,
)

_log = get_logger("argosy.allocation_author")


@dataclass
class AuthorOutcome:
    status: str                       # "accepted" | "rejected" | "unavailable"
    proposal: AllocationProposal | None = None
    report: GateReport | None = None
    attempts: int = 0


def run_allocation_author(
    packet: dict[str, Any],
    *,
    author_fn: Callable[[dict[str, Any], list | None], AllocationProposal | None],
    max_revisions: int = 2,
    verify: Callable[..., GateReport] = verify_allocation_proposal,
) -> AuthorOutcome:
    """Drive the author→verify→bounce loop and return the outcome. Never re-authors
    or mutates the proposal — it only relays the verifier's failures back to the
    author. The caller handles ``rejected``/``unavailable`` by degrading to the
    deterministic engine."""
    feedback: list | None = None
    last: GateReport | None = None
    attempts = 0
    for _ in range(max_revisions + 1):
        attempts += 1
        try:
            proposal = author_fn(packet, feedback)
        except Exception as exc:  # noqa: BLE001 — author unavailable (timeout/error)
            _log.warning("allocation_author.unavailable", error=str(exc)[:200])
            return AuthorOutcome(status="unavailable", attempts=attempts)
        if proposal is None:
            return AuthorOutcome(status="unavailable", attempts=attempts)

        report = verify(proposal, packet)
        last = report
        if report.status == GateStatus.ACCEPT:
            return AuthorOutcome(status="accepted", proposal=proposal,
                                 report=report, attempts=attempts)
        # BLOCK or REVISION_REQUIRED: relay the reasons and let the author revise.
        feedback = report.failures

    return AuthorOutcome(status="rejected", proposal=proposal, report=last, attempts=attempts)


__all__ = ["AuthorOutcome", "run_allocation_author"]
