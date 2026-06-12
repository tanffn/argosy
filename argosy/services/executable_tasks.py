"""ExecutableTask reconciliation gate (Slice 1b).

The allocation agent may only ORDER / GROUP / PACE / EXPLAIN the deterministic
1a candidates — it invents no numbers. This module enforces that: the agent's
task set must cover the candidate set EXACTLY (same identities, each used once,
none invented), keyed on the canonical :func:`candidate_fingerprint` (identity,
not notional-only). ``ExecutableTask`` itself is the cross-phase contract defined
in :mod:`argosy.services.contracts`; it is re-exported here so 1b consumers
import it alongside the gate.
"""
from __future__ import annotations

from argosy.services.contracts import (
    AllocationCandidate,
    ExecutableTask,
    candidate_fingerprint,
)


def reconcile_or_raise(tasks: list[ExecutableTask],
                       candidates: list[AllocationCandidate]) -> None:
    """Enforce that the task set is EXACTLY the candidate set — same identities,
    each used once, none invented (codex: identity + uniqueness + coverage)."""
    want: dict[tuple, int] = {}
    for c in candidates:
        fp = candidate_fingerprint(c)
        want[fp] = want.get(fp, 0) + 1
    got: dict[tuple, int] = {}
    for t in tasks:
        fp = candidate_fingerprint(t.candidate)
        if fp not in want:
            raise ValueError(
                f"task seq={t.seq} wraps an unknown/invented candidate {fp}")
        got[fp] = got.get(fp, 0) + 1
    if got != want:
        raise ValueError(
            "task set does not cover candidates 1:1 (missing/duplicated): "
            f"want={want} got={got}")


__all__ = ["ExecutableTask", "reconcile_or_raise"]
