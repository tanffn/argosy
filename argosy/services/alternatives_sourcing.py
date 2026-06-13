"""Thin service that gates an :class:`AlternativesProposal` through the
canonical domicile / estate-tax validator.

The agent (``argosy.agents.alternatives_sourcer``) PROPOSES instruments; this
service is the place those proposals are turned into typed
:class:`AllocationInstrument` rows and run through the SAME structured-object
gate the canonical plan uses
(:func:`argosy.services.target_allocation_doc.validate_instrument_domicile`).

This closes the failure class the doctrine warns about: a frozen / agent-proposed
instrument layer that sits OUTSIDE the estate gate silently rebuilds the US-situs
estate tail for a non-US-person. Here every proposed pick is validated; a RED
(US-situs) or YELLOW (unstamped domicile) instrument is EXCLUDED from the clean
set and surfaced as a violation — we never silently accept a US-situs pick.
"""

from __future__ import annotations

from argosy.agents.alternatives_sourcer import AlternativesProposal
from argosy.services.alternatives_types import VerifiedAlternativesCandidate
from argosy.logging import get_logger
from argosy.services.target_allocation_doc import (
    AllocationClassDoc,
    AllocationInstrument,
    DomicileViolation,
    TargetAllocationDoc,
    validate_instrument_domicile,
)

log = get_logger(__name__)

# The class label / sigma_class / snapshot anchor the Alternatives sleeve uses
# in the canonical doc — kept consistent with allocation_plan's sleeve so a
# gated proposal can be slotted into the canonical doc downstream.
_ALTERNATIVES_CLASS_LABEL = "Alternatives"
_ALTERNATIVES_SIGMA_CLASS = "alternatives"
_ALTERNATIVES_SNAPSHOT_CATEGORY = "Alternative"

# Domicile is a Literal on AllocationInstrument; map the agent's free-string
# domicile to the known set. Anything we don't recognise (incl. case noise) is
# left unstamped (None) so the validator YELLOW-flags it rather than the
# pydantic Literal raising — fail loud as a violation, never crash the gate.
_KNOWN_DOMICILES = {"US", "IE", "LU", "UK", "IL", "DE", "CH", "JE"}


def _normalise_domicile(raw: str | None) -> str | None:
    if raw is None:
        return None
    d = raw.strip().upper()
    return d if d in _KNOWN_DOMICILES else None


def _proposal_to_instrument(p) -> AllocationInstrument:
    """Convert one ``AssetProposal`` to a typed ``AllocationInstrument``.

    The instrument carries the proposal's symbol + within-sleeve weight +
    normalised domicile so the domicile gate can run on the structured object.
    The ISIN / asset_class / thesis are folded into ``rationale`` for the audit
    trail (the canonical instrument schema has no dedicated fields for them).
    """
    isin = f" ISIN {p.isin}" if p.isin else ""
    rationale = (
        f"[{p.asset_class}]{isin} (conviction={p.conviction}) {p.thesis_md}".strip()
    )
    return AllocationInstrument(
        symbol=p.symbol,
        role="primary",
        weight_within_class_pct=p.weight_within_sleeve_pct,
        rationale=rationale,
        domicile=_normalise_domicile(p.domicile),
    )


def verify_and_gate_proposal(
    proposal: AlternativesProposal,
) -> tuple[list[VerifiedAlternativesCandidate], list[str]]:
    """Verify each proposed instrument deterministically, THEN estate-gate the
    survivors. Returns ``(verified_candidates, violations)``.

    This supersedes the old claim-trusting gate. The pipeline is:

    1. :func:`verify_instrument` — deterministic ISIN checksum + domicile
       coherence + verified-facts registry. Only ``verified=True`` (GREEN) picks
       survive; a hallucinated ISIN, a US-situs prefix/domicile, or an
       unknown/unstamped instrument is rejected here and can never become a
       holding. Registry facts override the agent's claim.
    2. Estate gate (belt-and-suspenders) — the survivors are re-run through
       :func:`validate_instrument_domicile`; any that the canonical estate
       validator flags are dropped too. (GREEN already implies non-US, so this is
       redundant by design — defence in depth.)

    ``violations`` is a list of human-readable strings ``"<SYMBOL>: <SEVERITY> —
    <reason>"`` for every rejected pick, for the audit trail. Surviving
    candidates keep their proposed within-sleeve weights; re-normalising the
    weights of the survivors is the decision/engine step's responsibility.
    """
    from argosy.services.instrument_verification import (
        load_registry,
        registry_lookup,
        verify_instrument,
    )

    registry = load_registry()
    clean: list[VerifiedAlternativesCandidate] = []
    violations: list[str] = []

    for p in proposal.proposals:
        result = verify_instrument(
            symbol=p.symbol, claimed_domicile=p.domicile, claimed_isin=p.isin
        )
        if not result.verified:
            violations.append(f"{p.symbol}: {result.severity} — {result.reason}")
            continue
        # Registry is authoritative for a verified pick — use its stamped facts.
        hit = registry_lookup(p.symbol, registry) or {}
        clean.append(
            VerifiedAlternativesCandidate(
                symbol=p.symbol,
                name=p.name,
                asset_class=p.asset_class,
                domicile=str(hit.get("domicile", p.domicile)),
                isin=str(hit.get("isin", p.isin)),
                weight_within_sleeve_pct=p.weight_within_sleeve_pct,
                conviction=p.conviction,
                thesis_md=p.thesis_md,
                verification=result,
            )
        )

    # Belt-and-suspenders: estate-gate the survivors through the canonical doc.
    if clean:
        doc = TargetAllocationDoc(
            anchor_sigma=0.0,
            blended_sigma=0.0,
            nvda_cap_pct=0.0,
            fi_pct=0.0,
            provenance="alternatives_sourcer proposal (post-verify, pre-estate-gate)",
            classes=[
                AllocationClassDoc(
                    label=_ALTERNATIVES_CLASS_LABEL,
                    snapshot_category=_ALTERNATIVES_SNAPSHOT_CATEGORY,
                    sigma_class=_ALTERNATIVES_SIGMA_CLASS,
                    target_pct=proposal.sleeve_pct,
                    instruments=[_proposal_to_instrument(c) for c in clean],
                )
            ],
            glide=[],
        )
        estate_flags = validate_instrument_domicile(doc, non_us_person=True)
        if estate_flags:
            flagged = {v.symbol.upper() for v in estate_flags}
            for v in estate_flags:
                violations.append(f"{v.symbol}: {v.severity} (estate) — {v.reason}")
            clean = [c for c in clean if c.symbol.upper() not in flagged]

    if violations:
        log.warning(
            "alternatives_sourcing.verify_gate_rejected",
            total=len(proposal.proposals),
            clean=len(clean),
            rejected=len(violations),
        )

    return clean, violations


__all__ = ["verify_and_gate_proposal"]
