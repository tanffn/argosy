"""Gate outcomes — a verifier that did not run is not a verifier that passed.

Argosy's guardrails were fail-soft in three independent places (the codex
math gate, the whole-artifact coherence reader, the weekly digest). Each
returned "nothing" when it could not run, and each caller read "nothing" as
"fine". A plan was promoted with its blind math audit never executed; a
digest reported ``ok`` having sent no mail.

This module makes that impossible to express by accident. A gate reports one
of three states, and callers must handle the third:

    PASS         the check ran and the artifact is good
    BLOCK        the check ran and the artifact is bad
    DID_NOT_RUN  the check could not run (timeout, missing kit, unconfigured,
                 exception) — it says NOTHING about the artifact

For any promote / publish / deliver decision, ``DID_NOT_RUN`` is treated as
``BLOCK``: see :func:`blocks_promotion`. That is not a judgment about the
quality of a decision — the fleet owns judgment. It is the liveness floor:
we may not claim an artifact was verified when it was not.

Deliberate overrides remain cheap; SILENT overrides become impossible. An
operator may set ``override_by`` + ``override_reason``, which is persisted
and rendered on the verification receipt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "GateStatus",
    "GateOutcome",
    "blocks_promotion",
    "summarize",
]


class GateStatus(StrEnum):
    """Tri-state gate result. There is no "unknown means fine"."""

    PASS = "pass"
    BLOCK = "block"
    DID_NOT_RUN = "did_not_run"


@dataclass(frozen=True)
class GateOutcome:
    """One gate's verdict on one artifact.

    ``gate`` is a stable identifier (``"codex_math"``,
    ``"whole_artifact_reader"``, ``"publish_gate"``, ``"plan_invariants"``,
    ``"fx_freshness"``, ``"digest_send"``) so receipts stay comparable
    across runs.

    ``detail`` must explain a non-PASS well enough that a human reading the
    receipt knows what to do: ``"codex hung past 900s hard ceiling"`` beats
    ``"error"``.
    """

    gate: str
    status: GateStatus
    detail: str = ""
    override_by: str | None = None
    override_reason: str | None = None
    meta: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.gate:
            raise ValueError("GateOutcome.gate must be a non-empty identifier")
        if self.override_by and not self.override_reason:
            raise ValueError(
                "an override must carry a reason — silent overrides are the "
                "failure mode this module exists to prevent"
            )
        if self.status is not GateStatus.PASS and not self.detail:
            raise ValueError(
                f"gate {self.gate!r} reported {self.status} without a detail; "
                "a non-PASS must say why"
            )

    # -- constructors -----------------------------------------------------

    @classmethod
    def passed(cls, gate: str, detail: str = "", **meta: str) -> GateOutcome:
        return cls(gate=gate, status=GateStatus.PASS, detail=detail, meta=dict(meta))

    @classmethod
    def blocked(cls, gate: str, detail: str, **meta: str) -> GateOutcome:
        return cls(gate=gate, status=GateStatus.BLOCK, detail=detail, meta=dict(meta))

    @classmethod
    def did_not_run(cls, gate: str, detail: str, **meta: str) -> GateOutcome:
        """The check could not execute. This is NOT a pass."""
        return cls(
            gate=gate, status=GateStatus.DID_NOT_RUN, detail=detail, meta=dict(meta)
        )

    # -- queries ----------------------------------------------------------

    @property
    def overridden(self) -> bool:
        return self.override_by is not None

    def with_override(self, *, by: str, reason: str) -> GateOutcome:
        """Return a copy an operator has explicitly waved through.

        The original status is preserved so the receipt can say "BLOCK,
        overridden by ariel" rather than quietly reading PASS.
        """
        return GateOutcome(
            gate=self.gate,
            status=self.status,
            detail=self.detail,
            override_by=by,
            override_reason=reason,
            meta=dict(self.meta),
        )

    def blocks(self) -> bool:
        """True when this outcome must stop a promote / publish / deliver."""
        if self.overridden:
            return False
        return self.status is not GateStatus.PASS


def blocks_promotion(outcomes: list[GateOutcome]) -> list[GateOutcome]:
    """Outcomes that must stop promotion — BLOCK *and* DID_NOT_RUN.

    An empty result means every gate either passed or was explicitly
    overridden. Callers should refuse to promote when this is non-empty and
    render the reasons.
    """
    return [o for o in outcomes if o.blocks()]


def summarize(outcomes: list[GateOutcome]) -> str:
    """One line for logs and the plan header.

    ``"4/5 gates passed; whole_artifact_reader DID_NOT_RUN (codex hung)"``
    """
    total = len(outcomes)
    passed = sum(1 for o in outcomes if o.status is GateStatus.PASS)
    line = f"{passed}/{total} gates passed"
    problems = [
        f"{o.gate} {o.status.upper()} ({o.detail})"
        + (f" [overridden by {o.override_by}]" if o.overridden else "")
        for o in outcomes
        if o.status is not GateStatus.PASS
    ]
    if problems:
        line += "; " + "; ".join(problems)
    return line
