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


import re as _re

from argosy.quality.coherence.claim_markers import render_marker, parse_markers


def ensure_framing_marker(bodies: dict, subject_type: str, invariants: list[dict]) -> dict:
    """Idempotently translate an arbitrator's ``required_framing_role`` invariants
    into a typed claim marker embedded on each named markdown surface, so the
    deterministic verifier can check the framing ruling mechanically (it reads the
    marker, never the prose). A stale marker for the same subject is replaced; an
    identical one is left untouched (idempotent). ``forbidden_claim`` invariants need
    no patch — they are verify-only over the prose. Returns updated bodies."""
    roles_by_surface: dict[str, dict[str, str]] = {}
    for inv in invariants:
        if inv.get("kind") == "required_framing_role" and inv.get("subject_type") == subject_type:
            roles_by_surface.setdefault(inv.get("surface"), {})[inv["role_field"]] = inv["value"]
    out = dict(bodies)
    for surface, roles in roles_by_surface.items():
        text = out.get(surface, "") or ""
        if parse_markers(text).get(subject_type) == roles:
            continue  # already carries exactly this framing — no-op
        text = _re.sub(
            r"<!--coh:" + _re.escape(subject_type) + r"\s+[^>]*?-->", "", text
        ).rstrip()
        marker = render_marker(subject_type, roles)
        out[surface] = (text + "\n\n" + marker) if text else marker
    return out


from argosy.quality.coherence.dispute import cluster_findings
from argosy.quality.coherence.resolver_route import RouteKind, route_dispute

# Which owning roles argue each policy/framing subject on the panel.
_PANEL_ROLES: dict[str, list[str]] = {
    "retirement_age_headline": ["withdrawal_sequencer", "fi_methodology"],
}


@dataclass
class PassResult:
    ok: bool
    bodies: dict
    json_surfaces: dict
    rulings: list[dict] = field(default_factory=list)
    round_result: "CoherenceRoundResult | None" = None
    errors: list = field(default_factory=list)


def run_coherence_deliberation_pass(
    *, bodies: dict, json_surfaces: dict, findings: list[dict],
    canonical_facts: str, prime_directive: str,
    make_panelist=None, facilitator=None, arbitrator=None,
    resolver_value_fn=None, panel_roles: dict | None = None,
    allowed_numbers=frozenset(),
) -> PassResult:
    """End-to-end pass: cluster findings -> route -> (resolver | panel+arbitrator)
    -> conform every surface -> verify. Fail-closed: an un-typeable dispute, an
    unresolvable value dispute, a conform failure, or a verifier failure all yield
    ok=False (caller must BLOCK). Agents/resolver are injected so the pass is unit-
    testable; the live caller wires the real ones."""
    panel_roles = panel_roles or _PANEL_ROLES
    disputes = cluster_findings(findings)
    value_resolutions: dict[str, dict] = {}
    rulings: list[dict] = []
    work_bodies = dict(bodies)
    errors: list[str] = []

    for d in disputes:
        route = route_dispute(d)
        if route == RouteKind.BLOCK:
            errors.append(f"untypeable dispute (subject={d.subject_type!r}) -> BLOCK")
            continue
        if route == RouteKind.RESOLVER:
            if resolver_value_fn is None:
                errors.append(f"{d.subject_type}: no resolver wired for value dispute")
                continue
            res = resolver_value_fn(d)
            if not res:
                errors.append(f"{d.subject_type}: resolver could not resolve")
                continue
            value_resolutions[d.subject_type] = res
            rulings.append({
                "dispute_key": _dk(d), "subject_type": d.subject_type,
                "question": d.question, "ruling": "conformed to canonical value",
                "rationale": "deterministic resolver", "basis": "canonical_fact",
                "resolved_by": "resolver", "invariants": res.get("invariant", []),
                "conformed_surfaces": [p.get("surface_id") for p in res.get("patches", [])],
            })
            continue
        # ARBITRATION
        if facilitator is None or arbitrator is None or make_panelist is None:
            errors.append(f"{d.subject_type}: arbitration path not wired")
            continue
        positions = []
        for role in panel_roles.get(d.subject_type, []):
            pos = make_panelist(role).run_sync(
                represented_role=role, dispute_question=d.question,
                canonical_facts=canonical_facts, peer_positions=[p["position"] for p in positions],
            ).output
            positions.append({"role": role, "position": pos.position, "basis": pos.basis})
        result = deliberate_dispute(
            d, panelist_positions=positions, facilitator=facilitator, arbitrator=arbitrator,
            canonical_facts=canonical_facts, prime_directive=prime_directive,
        )
        work_bodies = ensure_framing_marker(work_bodies, d.subject_type, result.invariant)
        value_resolutions[d.subject_type] = {"patches": [], "invariant": result.invariant}
        rulings.append({
            "dispute_key": _dk(d), "subject_type": d.subject_type, "question": d.question,
            "ruling": result.ruling, "rationale": result.rationale, "basis": result.basis,
            "resolved_by": result.resolved_by, "invariants": result.invariant,
            "conformed_surfaces": sorted({
                i.get("surface") for i in result.invariant if i.get("surface")
            }),
        })

    if errors:
        return PassResult(False, bodies, json_surfaces, rulings, None, errors)

    rnd = run_coherence_round(
        bodies=work_bodies, json_surfaces=json_surfaces,
        value_resolutions=value_resolutions, allowed_numbers=allowed_numbers,
    )
    return PassResult(rnd.ok, rnd.bodies, rnd.json_surfaces, rulings, rnd,
                      [] if rnd.ok else list(rnd.verifier.failures))


def _dk(d) -> str:
    from argosy.quality.coherence.dispute import dispute_key
    return dispute_key(d)
