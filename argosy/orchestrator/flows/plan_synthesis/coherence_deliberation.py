# argosy/orchestrator/flows/plan_synthesis/coherence_deliberation.py
"""Per-dispute deliberation: panel positions -> facilitator -> (arbitrator on
no-consensus). Returns the ruling + typed invariant. The agents are injected so the
loop is unit-testable without a live model. The full-run wiring (cluster -> route ->
resolver/deliberate -> conform -> verify -> ledger -> re-read) is assembled in
Task 5.5 and called from orchestrator.py."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from argosy.quality.coherence.dispute import Dispute


@dataclass
class DeliberationResult:
    resolved_by: str                      # "consensus" | "arbitrator"
    ruling: str
    rationale: str
    basis: str
    invariant: list[dict[str, Any]] = field(default_factory=list)
    per_surface_instructions: list[dict[str, Any]] = field(default_factory=list)


def deliberate_dispute(
    dispute: Dispute, *, panelist_positions: list[dict], facilitator, arbitrator,
    canonical_facts: str, prime_directive: str,
) -> DeliberationResult:
    fac = facilitator.run_sync(
        dispute_question=dispute.question, positions=panelist_positions
    ).output
    if getattr(fac, "consensus", False):
        return DeliberationResult(
            resolved_by="consensus", ruling=fac.ruling, rationale="panel consensus",
            basis="canonical_fact",
        )
    ruling = arbitrator.run_sync(
        dispute_question=dispute.question, positions=panelist_positions,
        canonical_facts=canonical_facts, prime_directive=prime_directive,
    ).output
    return DeliberationResult(
        resolved_by="arbitrator", ruling=ruling.ruling_statement,
        rationale=ruling.rationale, basis=ruling.basis,
        invariant=list(ruling.coherence_invariant),
        per_surface_instructions=list(ruling.per_surface_instructions),
    )


import json as _json

from argosy.quality.coherence.conformer import ConformPatch, apply_patches
from argosy.quality.coherence.invariants import (
    EqualsCanonical, AllRegisteredSurfacesPresent, RequiredFramingRole,
    ForbiddenClaim, SurfaceClaimEquals, verify_invariants, VerifyResult,
)

_INV_CLASSES = {
    "equals_canonical": EqualsCanonical,
    "all_registered_surfaces_present": AllRegisteredSurfacesPresent,
    "required_framing_role": RequiredFramingRole,
    "forbidden_claim": ForbiddenClaim,
    "surface_claim_equals": SurfaceClaimEquals,
}


def _build_invariant(spec: dict):
    kind = spec.get("kind")
    cls = _INV_CLASSES.get(kind)
    if cls is None:
        raise ValueError(f"unknown invariant kind: {kind!r}")
    return cls(**{k: v for k, v in spec.items() if k != "kind"})


@dataclass
class CoherenceRoundResult:
    ok: bool
    bodies: dict
    json_surfaces: dict
    verifier: VerifyResult
    errors: list = field(default_factory=list)


def run_coherence_round(
    *, bodies: dict, json_surfaces: dict,
    value_resolutions: dict[str, dict], allowed_numbers=frozenset(),
) -> CoherenceRoundResult:
    """Apply all resolved patches across surfaces atomically, then verify every
    invariant. Fail-closed: any conform or verify failure => ok=False."""
    patches: list[ConformPatch] = []
    invariants = []
    for _subject, res in value_resolutions.items():
        for p in res.get("patches", []):
            patches.append(ConformPatch(**p))
        for inv in res.get("invariant", []):
            invariants.append(_build_invariant(inv))

    conform = apply_patches(bodies, json_surfaces, patches, allowed_numbers=allowed_numbers)
    if not conform.ok:
        return CoherenceRoundResult(False, bodies, json_surfaces,
                                    VerifyResult(False, conform.errors), conform.errors)

    artifact = dict(conform.bodies)
    for sid, payload in conform.json_surfaces.items():
        artifact[f"{sid}_text"] = _json.dumps(payload, ensure_ascii=False)
        artifact[sid] = _json.dumps(payload, ensure_ascii=False)
    verres = verify_invariants(invariants, artifact)
    return CoherenceRoundResult(verres.ok, conform.bodies, conform.json_surfaces, verres)
