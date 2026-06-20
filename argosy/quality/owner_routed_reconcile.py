"""Phase 3c — the owner-routed reconcile ROUND.

The closing piece of the firm model: a whole-artifact reader BLOCK is not a veto on
the whole plan, it is a set of findings — each OWNED by exactly one role. This module
runs ONE bounded reconcile round end-to-end:

  1. route each BLOCKER finding to its owner (``findings_to_change_requests``):
       * figure objections — the finding targets a single canonical figure node;
       * prose-routed      — an owner but no single figure (instrument membership,
                             execution-gate, vest policy) → the owner fixes prose;
       * unroutable        — no routable subject.
  2. for the FIGURE objections, ask each owner to propose a targeted fix
     (``propose_remediations`` → set_value / prose_fix / decline).
  3. apply every PROSE fix (owner prose_fix + prose-routed findings + a catch-all
     "Lead integrates" pass over any unroutable finding that still cites a surface)
     to the draft bodies via the segment-level surgical editor — span-local, no new
     numbers.

PROSE is the ONLY artifact mutation this round makes. A FIGURE value change (the
owner judged the number itself wrong) is SURFACED in ``value_change_requests`` —
NOT applied here. Applying a figure change to the graph without re-rendering every
surface that displays it would recreate the exact cross-surface contradiction this
redesign exists to prevent (and a derived figure is not a valid direct input edit
anyway). A genuine figure correction is a deeper change that belongs to the caller's
full-cycle / re-synth fallback, which regenerates every surface coherently. Hence
``made_progress`` is gated on prose edits actually spliced — a round that only
declines / only surfaces a figure change made NO body progress and the caller falls
through to that fallback.

Targeted, not whole-monolith regen — that is what lets the plan converge. Pure
orchestration: the owner proposer and the prose editor are injected, so this module
is deterministic + unit-testable. The caller re-runs the reader after the round.

See docs/superpowers/specs/2026-06-19-financial-advisory-team-design.md (Phase 3).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from argosy.orchestrator.flows.plan_synthesis.surgical_reconcile import (
    surgically_correct_draft,
)
from argosy.quality.finding_remediation import propose_remediations
from argosy.quality.finding_router import findings_to_change_requests


@dataclass
class OwnerRoutedRoundResult:
    """Outcome of one owner-routed reconcile round."""

    bodies: dict                                   # corrected {long, medium, short}
    value_change_requests: list = field(default_factory=list)  # figure changes SURFACED (not applied)
    prose_edits: list = field(default_factory=list)            # SurgicalEdit actually spliced
    declines: list = field(default_factory=list)               # owner declines (recorded)
    unaddressed: list = field(default_factory=list)            # prose findings the editor could not splice
    unowned: list = field(default_factory=list)                # no owner AND no fixable surface

    @property
    def made_progress(self) -> bool:
        """True ONLY when a prose edit was actually spliced into the body — the single
        artifact change this round makes. A figure value-change is surfaced, not
        applied (it doesn't mutate the body the reader reads), so it is NOT progress
        here; nor is a decline / an unowned finding. A no-progress round signals the
        caller to fall through to the full-cycle / re-synth fallback rather than loop."""
        return bool(self.prose_edits)


class _ProseFinding:
    """A minimal finding shape for the surgical editor (kind / detail / surfaces).

    The owner's INSTRUCTION (for an owner prose_fix) or the reader's DETAIL (for a
    prose-routed / Lead-caught finding) becomes the editor's defect_reason. ``kind`` is
    forced to a fixable coherence kind so the editor always attempts the cited span."""

    def __init__(self, *, detail: str, surfaces_cited):
        self.kind = "contradiction"
        self.detail = detail or ""
        self.surfaces_cited = list(surfaces_cited or [])


def run_owner_routed_reconcile_round(
    *,
    reader_verdict,
    bodies: dict,
    graph,
    proposer,
    resolved=None,
    editor=None,
    severities: tuple[str, ...] = ("BLOCKER",),
) -> OwnerRoutedRoundResult:
    """Run ONE owner-routed reconcile round over a reader verdict. See module docstring.

    ``bodies`` is ``{"long": md, "medium": md, "short": md}``. ``proposer`` is the owner
    remediation seam (live agent in prod, a double in tests); ``editor`` is the prose
    editor passthrough for the surgical splice (None → the real ProseEditorAgent)."""
    figure_crs, prose_routed, unroutable = findings_to_change_requests(
        reader_verdict, severities=severities
    )

    # FIGURE objections → ask each owner for a targeted fix.
    remediation = propose_remediations(figure_crs, proposer=proposer, graph=graph)

    # Assemble the PROSE-fix work list:
    #   * owner prose_fix proposals (figure is right, narrative drifted),
    #   * prose-routed findings (owner, but no single figure → fix prose directly),
    #   * a catch-all Lead pass over any unroutable finding that still cites a surface
    #     (nothing silently dropped; a no-surface unroutable is genuinely unowned).
    prose_findings: list[_ProseFinding] = []
    for pf in remediation.prose_fixes:
        prose_findings.append(_ProseFinding(
            detail=pf.get("instruction") or pf.get("rationale") or "",
            surfaces_cited=pf.get("surfaces_cited"),
        ))
    for r in prose_routed:
        prose_findings.append(_ProseFinding(
            detail=r.detail, surfaces_cited=r.surfaces_cited,
        ))
    unowned: list = []
    for f in unroutable:
        surfaces = getattr(f, "surfaces_cited", None) or []
        if surfaces:
            prose_findings.append(_ProseFinding(
                detail=getattr(f, "detail", "") or "", surfaces_cited=surfaces,
            ))
        else:
            unowned.append(f)

    # Coalesce findings that cite the SAME surface so the editor never edits a span
    # twice (after the first splice the second wouldn't find it → silently dropped).
    # Merge their defect reasons so the single edit addresses both concerns.
    prose_findings = _coalesce_by_surface(prose_findings)

    prose_edits: list = []
    unaddressed: list = []
    corrected = dict(bodies or {})
    if prose_findings:
        surg = surgically_correct_draft(
            bodies=corrected,
            reader_verdict=_ProseVerdict(prose_findings),
            resolved=resolved,
            editor=editor,
        )
        corrected = surg.corrected_bodies
        prose_edits = surg.edits
        unaddressed = surg.unaddressed

    return OwnerRoutedRoundResult(
        bodies=corrected,
        value_change_requests=list(remediation.value_change_requests),
        prose_edits=prose_edits,
        declines=list(remediation.declines),
        unaddressed=unaddressed,
        unowned=unowned,
    )


def _coalesce_by_surface(prose_findings: list) -> list:
    """Merge prose findings that cite an identical surface into one (combined detail),
    so a span is edited at most once. First occurrence keeps position; a later finding
    citing only already-seen surfaces is folded into the earlier one's detail."""
    by_key: dict[tuple, _ProseFinding] = {}
    order: list[_ProseFinding] = []
    for f in prose_findings:
        key = tuple(f.surfaces_cited)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = f
            order.append(f)
        elif f.detail and f.detail not in existing.detail:
            existing.detail = f"{existing.detail} | {f.detail}".strip(" |")
    return order


class _ProseVerdict:
    """A minimal verdict shape (``.findings``) for ``surgically_correct_draft``."""

    def __init__(self, findings):
        self.findings = findings


__all__ = ["OwnerRoutedRoundResult", "run_owner_routed_reconcile_round"]
