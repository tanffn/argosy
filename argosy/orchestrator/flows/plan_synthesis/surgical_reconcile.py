"""Segment-level surgical reconcile — fix a blocked draft by editing ONLY the
reader's cited offending snippets, not by re-running the whole synthesizer.

The reconcile loop's old behavior re-ran the full phase-3 synthesizer per reader
BLOCK (~45 min, stochastic → reshuffles & re-introduces defects → does not
converge; proven by run 106/107). This routine instead:

  for each fixable reader finding (it cites verbatim offending excerpts):
    - prose-edit JUST that excerpt, grounded in the canonical resolver context,
      via the cheap ProseEditorAgent (one small LLM call per snippet);
    - splice the corrected snippet into the draft body in place.

Editing the cited segment (not the whole plan) is what lets the fix converge:
it cannot reshuffle the rest of the document. Number facts with a deterministic
render site are handled by ``fact_correction.rerender_deterministic_sites``; this
module covers the free-text contradictions (FI sufficiency, liquidity runway,
stale dates) that are the realistic majority of reader blocks.

The editor is injectable (tests pass a stub → fully deterministic, no LLM/DB).
The caller re-verifies (deterministic gate + a fresh whole-artifact read) after.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)

# Coherence finding kinds the segment editor can act on (mirror the reconcile
# loop's _READER_COHERENCE_KINDS). An 'other' finding with no cited surface is a
# reader infra-failure, not a fixable segment.
_FIXABLE_KINDS = frozenset(
    {"contradiction", "cross_surface", "fragile_claim", "stale", "regression"}
)


@dataclass
class SurgicalEdit:
    horizon: str
    before: str
    after: str
    finding_kind: str


@dataclass
class SurgicalResult:
    corrected_bodies: dict
    edits: list = field(default_factory=list)
    addressed: list = field(default_factory=list)      # findings a segment edit touched
    unaddressed: list = field(default_factory=list)    # structural / uncited → caller falls back


def _attr(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _is_fixable(finding) -> bool:
    kind = (_attr(finding, "kind", "") or "").lower()
    if kind in _FIXABLE_KINDS:
        return True
    # an 'other' finding is fixable only if it quotes a concrete surface
    return kind == "other" and bool(_attr(finding, "surfaces_cited", None))


def resolver_context(resolved) -> str:
    """Compact authoritative-facts block the editor grounds corrections in.

    Best-effort: pulls a handful of key resolved values into a short text block.
    Never raises (a missing resolver degrades to an empty context)."""
    if resolved is None:
        return "(no resolver manifest available)"
    keys = (
        "portfolio.net_worth_nis", "portfolio.usd_exposure_nis",
        "retirement.fi_target_nis", "retirement.fi_total_capital_nis",
        "retirement.fi_margin_signed_nis", "retirement.earliest_safe_age",
        "retirement.fi_age", "concentration.nvda_current_pct",
        "concentration.nvda_cap_pct",
    )
    lines: list[str] = []
    for k in keys:
        try:
            rv = resolved.get(k)
        except Exception:  # noqa: BLE001
            rv = None
        if rv is not None and _attr(rv, "status", None) == "resolved":
            val = _attr(rv, "value", None)
            if val is not None:
                lines.append(f"- {k} = {val}")
    return "\n".join(lines) if lines else "(no resolved values)"


def surgically_correct_draft(
    *,
    bodies: dict,
    reader_verdict,
    resolved=None,
    editor: Callable[[str], str] | None = None,
) -> SurgicalResult:
    """Apply segment-level prose corrections for each fixable reader finding.

    ``bodies`` is ``{"long": md, "medium": md, "short": md}`` (the draft's
    user-facing horizon markdown). Returns a :class:`SurgicalResult` with the
    corrected bodies, the edits applied, and the findings addressed vs left for
    the full-resynth fallback. Each cited excerpt that is a verbatim substring of
    a body is replaced by the editor's minimal correction; a non-matching excerpt
    (e.g. from the dashboard/appendix) is skipped here (idempotent)."""
    from argosy.agents.prose_editor import correct_prose_site

    corrected = dict(bodies or {})
    edits: list[SurgicalEdit] = []
    addressed: list[Any] = []
    unaddressed: list[Any] = []
    context = resolver_context(resolved)

    findings = _attr(reader_verdict, "findings", None) or []
    for finding in findings:
        if not _is_fixable(finding):
            unaddressed.append(finding)
            continue
        kind = (_attr(finding, "kind", "") or "").lower()
        detail = _attr(finding, "detail", "") or ""
        excerpts = _attr(finding, "surfaces_cited", None) or []
        touched = False
        for ex in excerpts:
            if not ex:
                continue
            for hz in ("long", "medium", "short"):
                body = corrected.get(hz) or ""
                if ex in body:
                    fixed = correct_prose_site(
                        fact_id=f"reader.{kind}", canonical_value=context,
                        offending_text=ex, defect_reason=detail, editor=editor,
                    )
                    if fixed and fixed != ex:
                        corrected[hz] = body.replace(ex, fixed, 1)
                        edits.append(SurgicalEdit(horizon=hz, before=ex, after=fixed, finding_kind=kind))
                        touched = True
                    break  # excerpt found in this horizon — done with it
        (addressed if touched else unaddressed).append(finding)

    log.info(
        "surgical_reconcile.applied edits=%d addressed=%d unaddressed=%d",
        len(edits), len(addressed), len(unaddressed),
    )
    return SurgicalResult(
        corrected_bodies=corrected, edits=edits,
        addressed=addressed, unaddressed=unaddressed,
    )
