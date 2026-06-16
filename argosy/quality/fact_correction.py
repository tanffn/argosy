"""Fact-level surgical correction — fix a finding at its canonical fact + every
render site at once, so the contradiction cannot move.

- ``template`` / ``structured_field`` sites are produced FROM the canonical
  value, so they re-render deterministically when the fact changes.
- ``llm_prose`` sites are authored free text; they route to the prose editor.

Re-verify runs the FULL deterministic suite (global, by design — FI-shock and
coherence read artifact-wide), then the whole-artifact reader stays as the net.
Pure functions except ``reverify_corrected`` which calls the deterministic gate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from argosy.quality.fact_ledger import FactLedger, RenderedFactSite, SiteKind


@dataclass(frozen=True)
class CorrectionPatch:
    """A deterministic re-render of one site from the canonical value."""

    site: RenderedFactSite
    new_text: str


def _fmt(v: Any) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _retext(old_text: str, old_value: Any, new_value: Any) -> str:
    """Replace the old value token in a site's rendered_text with the new one,
    tolerating int/float formatting ('13' / '13.0')."""
    new_s = _fmt(new_value)
    out = old_text
    for token in {_fmt(old_value), str(old_value)}:
        if token and token in out:
            out = out.replace(token, new_s)
    return out


def rerender_deterministic_sites(
    fact_id: str, new_value: Any, ledger: FactLedger
) -> list[CorrectionPatch]:
    """Re-render every template/structured_field site of ``fact_id`` from the new
    canonical value. llm_prose sites are skipped (they go to the prose editor)."""
    patches: list[CorrectionPatch] = []
    for site in ledger.sites_for_fact(fact_id):
        if site.site_kind == SiteKind.LLM_PROSE:
            continue
        new_text = _retext(site.rendered_text, site.normalized_value, new_value)
        patches.append(CorrectionPatch(site=site, new_text=new_text))
    return patches


def apply_text_corrections(
    artifact_text: str,
    patches: list[CorrectionPatch],
    prose_edits: list[tuple[str, str]],
) -> str:
    """Apply deterministic patches (replace each site's old rendered_text with
    its new_text) + prose edits (offending→corrected) to the assembled artifact.

    Text-replacement based (the render sites are raw markdown segments, not
    offset-addressable structured fields — see the render.py prose sites). A
    patch/edit whose source text is absent is skipped (idempotent, never raises).
    """
    out = artifact_text or ""
    for p in patches:
        if p.site.rendered_text and p.site.rendered_text in out:
            out = out.replace(p.site.rendered_text, p.new_text)
    for old, new in prose_edits or []:
        if old and old in out:
            out = out.replace(old, new)
    return out


def route_finding(finding, ledger: FactLedger) -> Literal["surgical", "structural"]:
    """Decide the fix path. A finding whose locations ALL attribute to a concrete
    fact_id is surgically renderable; an unattributable (fact_id=None / structural
    scope) finding routes to full re-synthesis (the derivation, not the rendering,
    is suspect). Uses Slice-2 ledger attribution."""
    from argosy.quality.fact_attribution import attribute_finding

    locs = attribute_finding(finding, ledger)
    if locs and all(loc.fact_id is not None and loc.scope != "structural" for loc in locs):
        return "surgical"
    return "structural"


def reverify_corrected(corrected_text: str, *, gate_kwargs: dict | None = None):
    """Re-run the FULL deterministic gate suite on the corrected artifact.

    The suite is global by design (FI-shock + coherence + IPS read artifact-
    wide), so a surgical patch is re-checked against the WHOLE document, never a
    section. The whole-artifact LLM reader stays as the holistic net downstream;
    this is the deterministic half. ``gate_kwargs`` forwards optional inputs
    (today, snapshot_date, resolved, fx, caps, target_allocation_doc, ...)."""
    from argosy.quality.plan_output_gate import gate_plan_output

    kwargs = dict(gate_kwargs or {})
    return gate_plan_output(horizon_text={"long": corrected_text}, **kwargs)
