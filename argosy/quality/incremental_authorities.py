"""Scoped authority composition for an incremental (edit-in-place) plan.

The promotion gate runs the FULL authority set — codex / deterministic_gate /
fund_manager / whole_artifact_reader / rederivation — fail-closed, identical for
steady-state promotion and migration baseline (spec Layers 3-4). An incremental
plan does NOT inherit synthesis-run verdicts; it EARNS them by running the
authorities, with codex/reader scoped to the CHANGED nodes (targeted, not the
95-min fleet).

This module composes the 5-key authorities dict that ``publish_gate`` /
``promote_gate`` consume:

  * The two DETERMINISTIC authorities (``deterministic_gate``, ``rederivation``)
    are passed in as booleans — the caller computes them from the rendered
    artifact (plan_output_gate) + the resolver/HEADLINE_NUMERIC_SOURCE check.
  * The three LLM authorities (``codex``, ``whole_artifact_reader``,
    ``fund_manager``) come from an injected ``AuthorityAgents`` protocol — a real
    implementation makes scoped agent calls; tests inject a deterministic fake.

Pure composition: no DB, no LLM here. Fail-closed — a missing/None verdict stays
None so ``promote_gate`` blocks on it.
"""
from __future__ import annotations

from typing import Any, Protocol


class AuthorityAgents(Protocol):
    """The LLM seam for the scoped authority run. Production wires real agents
    (codex re-derivation / whole-artifact reader / FM), each scoped to the
    changed nodes; tests inject a deterministic double."""

    def codex_rederive(
        self, *, plan_fields: dict[str, Any], changed_node_keys: list[str]
    ) -> str | None:
        """codex re-derivation verdict (APPROVE / APPROVE_WITH_CONDITIONS /
        BLOCK / None), scoped to the changed derived-value nodes."""
        ...

    def reader_review(self, *, plan_fields: dict[str, Any]) -> str | None:
        """Whole-artifact reader verdict (APPROVE / BLOCK / None)."""
        ...

    def fund_manager_review(self, *, plan_fields: dict[str, Any]) -> str | None:
        """FM verdict (approved / rejected / None)."""
        ...


def compute_incremental_authorities(
    *,
    agents: AuthorityAgents,
    plan_fields: dict[str, Any],
    changed_node_keys: list[str],
    deterministic_gate_clear: bool,
    rederivation_clear: bool,
) -> dict[str, object]:
    """Compose the 5-key authorities dict for ``publish_gate.can_publish_plan``.

    The three LLM verdicts come from ``agents`` (scoped to ``changed_node_keys``);
    the two deterministic ones from the caller's booleans. A None LLM verdict is
    passed through unchanged so ``promote_gate`` fails closed on it (never
    silently cleared)."""
    return {
        "codex": agents.codex_rederive(
            plan_fields=plan_fields, changed_node_keys=list(changed_node_keys)
        ),
        "deterministic_gate": bool(deterministic_gate_clear),
        "fund_manager": agents.fund_manager_review(plan_fields=plan_fields),
        "whole_artifact_reader": agents.reader_review(plan_fields=plan_fields),
        "rederivation": "APPROVE" if rederivation_clear else "BLOCK",
    }


__all__ = ["AuthorityAgents", "compute_incremental_authorities"]
