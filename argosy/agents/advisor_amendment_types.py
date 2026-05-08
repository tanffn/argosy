"""Pydantic types for the advisor's plan-amendment-chat flow (Wave 4).

The advisor's structured turn output gains an `amendment` field of type
`AmendmentIntent | None`. The API route reads that and emits an
`AmendmentResultDTO` to the chat client.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from argosy.agents.plan_synthesizer_types import Delta


class AmendmentIntent(BaseModel):
    """Advisor's classification of a chat-borne plan amendment request.

    The advisor emits this in its structured turn output when it judges
    the latest user message asks for a plan change. The dispatcher
    reads it and routes:
      - tier="small" + direction="tighten" + proposed_delta -> apply inline
      - tier="small" + direction in {"loosen","ambiguous"} -> escalate to medium
      - tier="medium" -> dispatch lightweight synth worker
      - tier="large" -> dispatch full synth worker
    """

    tier: Literal["small", "medium", "large"]
    direction: Literal["tighten", "loosen", "ambiguous"] | None = None
    proposed_delta: Delta | None = None
    rationale: str
    requires_confirmation: bool = False
    # When True, the dispatcher cancels any in-flight amendment for this
    # user before opening a new one (instead of returning
    # `needs_confirmation`). Set by the route layer when the user has
    # explicitly answered "yes, cancel and restart" in a prior chat turn.
    cancel_existing: bool = False


class AmendmentResultDTO(BaseModel):
    """API surface emitted on `POST /api/advisor/turn` when the turn
    classified an amendment.

    Status semantics:
      - "applied": Small Delta was applied; draft_id points at the affected draft.
      - "running": Medium/Large worker dispatched; decision_run_id and eta_seconds populated.
      - "needs_confirmation": concurrency conflict or ambiguous direction;
        advisor's turn text asks the user to clarify.
      - "cancelled_existing": user said "cancel and restart"; the prior
        run is cancelled, this turn confirms.
    """

    tier: Literal["small", "medium", "large"]
    decision_run_id: int
    status: Literal["applied", "running", "needs_confirmation", "cancelled_existing"]
    draft_id: int | None = None
    eta_seconds: int | None = None


__all__ = ["AmendmentIntent", "AmendmentResultDTO"]
