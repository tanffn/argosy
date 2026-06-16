"""Canonical facts + the render-site ledger (the fact-centric design keystone).

A *canonical fact* is a single load-bearing value (the retirement age, the FX
rate, the NVDA cap, a sleeve weight) with a stable ``fact_id`` and one derived
value. A ``RenderedFactSite`` records WHERE that fact was rendered — emitted at
render time FROM the canonical value, never reverse-engineered from finished
text (an excerpt hash proves a string existed, not which fact it expresses).

Pure data + indexing. No I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SiteKind(str, Enum):
    """How a render site was produced — determines if it can be deterministically
    re-rendered (Slice 3)."""

    TEMPLATE = "template"              # produced from the canonical value via a template
    STRUCTURED_FIELD = "structured_field"  # a typed field set from the canonical value
    LLM_PROSE = "llm_prose"           # free LLM-authored text; not deterministically re-renderable


@dataclass(frozen=True)
class Fact:
    """A canonical derived value, owned by its derivation (not by a section)."""

    fact_id: str
    value: object
    unit: str | None = None
    derivation: str | None = None  # e.g. "resolver:retirement.fi_age" / "doc:nvda_cap_pct"


@dataclass(frozen=True)
class RenderedFactSite:
    """One place a fact was rendered, recorded at render time."""

    fact_id: str
    surface_id: str           # body|dashboard|appendix|target_allocation_json|fm_objection|prior_plan
    field_path: str | None    # json_path / section_id+offset / table cell
    byte_span: tuple[int, int]
    rendered_text: str
    normalized_value: object
    site_kind: SiteKind
    hash: str


@dataclass
class FactLedger:
    """Indexed collection of render sites. Built up as renderers emit sites."""

    sites: list[RenderedFactSite] = field(default_factory=list)

    def add(self, site: RenderedFactSite) -> None:
        self.sites.append(site)

    def extend(self, sites: list[RenderedFactSite]) -> None:
        self.sites.extend(sites)

    def sites_for_fact(self, fact_id: str) -> list[RenderedFactSite]:
        return [s for s in self.sites if s.fact_id == fact_id]

    def sites_for_surface(self, surface_id: str) -> list[RenderedFactSite]:
        return [s for s in self.sites if s.surface_id == surface_id]

    def fact_ids(self) -> set[str]:
        return {s.fact_id for s in self.sites}
