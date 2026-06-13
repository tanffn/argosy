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


def gate_proposal(
    proposal: AlternativesProposal,
) -> tuple[list[AllocationInstrument], list[DomicileViolation]]:
    """Gate an ``AlternativesProposal`` through the estate / domicile validator.

    Builds a minimal :class:`TargetAllocationDoc` containing a single
    Alternatives class with the proposed instruments, runs
    :func:`validate_instrument_domicile`, and returns
    ``(clean_instruments, violations)`` where:

    - ``clean_instruments`` are the instruments with NO violation (estate-safe,
      domicile-stamped, non-US-situs) — admissible for the canonical sleeve;
    - ``violations`` are the RED (US-domiciled / US-situs) and YELLOW
      (unstamped domicile) flags. Any flagged instrument is EXCLUDED from
      ``clean_instruments`` — we fail loud and never silently accept a US-situs
      pick.

    The sanctioned-US-situs set is left at its default (NVDA only); the
    Alternatives sleeve has no business proposing NVDA, so any US-domiciled pick
    here is unconditionally RED.
    """
    instruments = [_proposal_to_instrument(p) for p in proposal.proposals]

    doc = TargetAllocationDoc(
        anchor_sigma=0.0,
        blended_sigma=0.0,
        nvda_cap_pct=0.0,
        fi_pct=0.0,
        provenance="alternatives_sourcer proposal (pre-gate)",
        classes=[
            AllocationClassDoc(
                label=_ALTERNATIVES_CLASS_LABEL,
                snapshot_category=_ALTERNATIVES_SNAPSHOT_CATEGORY,
                sigma_class=_ALTERNATIVES_SIGMA_CLASS,
                target_pct=proposal.sleeve_pct,
                instruments=instruments,
            )
        ],
        glide=[],
    )

    violations = validate_instrument_domicile(doc, non_us_person=True)
    flagged_symbols = {v.symbol.upper() for v in violations}
    clean = [i for i in instruments if i.symbol.upper() not in flagged_symbols]

    if violations:
        log.warning(
            "alternatives_sourcing.gate_rejected",
            total=len(instruments),
            clean=len(clean),
            violations=len(violations),
            red=sum(1 for v in violations if v.severity == "RED"),
            yellow=sum(1 for v in violations if v.severity == "YELLOW"),
            symbols=sorted(flagged_symbols),
        )

    return clean, violations


__all__ = ["gate_proposal"]
