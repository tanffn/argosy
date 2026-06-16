"""Deterministic RenderedFactSite emission for the allocation facts.

The allocation facts (NVDA cap, each sleeve weight) are produced FROM the
canonical TargetAllocationDoc, so their render sites can be emitted
deterministically — the keystone proof that the ledger is recorded from
canonical values, not reverse-engineered from finished prose. ``llm_prose``
allocation sites (e.g. a sentence paraphrasing the weights) are NOT covered
here; they are Slice 3's prose-editor scope.

Pure function, no I/O. Duck-types the doc (label / target_pct / nvda_cap_pct).
"""
from __future__ import annotations

import hashlib

from argosy.quality.fact_ledger import RenderedFactSite, SiteKind


def _hash(*parts: object) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


def build_allocation_fact_sites(doc, resolved=None) -> list[RenderedFactSite]:
    """Emit RenderedFactSite entries for the canonical allocation facts."""
    if doc is None:
        return []
    sites: list[RenderedFactSite] = []

    cap = getattr(doc, "nvda_cap_pct", None)
    if cap is not None:
        cap = float(cap)
        sites.append(RenderedFactSite(
            fact_id="allocation.nvda_cap_pct", surface_id="target_allocation_json",
            field_path="$.nvda_cap_pct", byte_span=(0, 0),
            rendered_text=f"{cap}", normalized_value=cap,
            site_kind=SiteKind.STRUCTURED_FIELD, hash=_hash("cap", cap),
        ))

    for i, cls in enumerate(getattr(doc, "classes", None) or []):
        label = getattr(cls, "label", None)
        pct = getattr(cls, "target_pct", None)
        if label is None or pct is None:
            continue
        pct = float(pct)
        sites.append(RenderedFactSite(
            fact_id="allocation.target_weights", surface_id="target_allocation_json",
            field_path=f"$.classes[{i}].target_pct", byte_span=(0, 0),
            rendered_text=f"{label} {pct}%", normalized_value=pct,
            site_kind=SiteKind.STRUCTURED_FIELD, hash=_hash("weight", label, pct),
        ))
    return sites
