"""Attribute a reader/gate finding to canonical fact(s) via the render ledger.

The keystone safety property: attribution uses the RenderedFactSite ledger (a
fact→site mapping recorded at render time), NOT a bare excerpt-hash lookup (an
excerpt proves a string existed, not which fact it expresses). A finding that
cannot be attributed is FAIL-SAFE: it routes to full re-synthesis and is logged
as an attribution gap (never silently dropped).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from argosy.quality.fact_ledger import FactLedger
from argosy.quality.fact_inventory import RUN106_FACTS

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FindingLocation:
    """Typed locator (replaces the optional ``GateViolation.locator`` string)."""

    check: str | None        # GateCheck value / invariant_id / reader kind
    fact_id: str | None      # the canonical fact, when attributable
    surface_id: str          # body|dashboard|...|"unattributed"
    field_path: str | None
    excerpt_hash: str | None
    scope: str               # "current" | "prior" | "structural"


def _excerpts(finding) -> list[str]:
    cited = finding.get("surfaces_cited") if isinstance(finding, dict) else getattr(finding, "surfaces_cited", None)
    return list(cited or [])


def attribute_finding(finding, ledger: FactLedger, *, inventory=RUN106_FACTS) -> list[FindingLocation]:
    """Map ``finding`` to FindingLocation[] via the ledger. Multi-owner allowed;
    unattributable → a single structural-route location + a logged gap."""
    locs: list[FindingLocation] = []
    excerpts = _excerpts(finding)
    kind = finding.get("kind") if isinstance(finding, dict) else getattr(finding, "kind", None)

    for site in ledger.sites:
        for ex in excerpts:
            if not ex:
                continue
            # text or normalized-value match ties the excerpt to this fact's site
            if site.rendered_text and (site.rendered_text in ex or str(site.normalized_value) in ex):
                locs.append(FindingLocation(
                    check=kind, fact_id=site.fact_id, surface_id=site.surface_id,
                    field_path=site.field_path, excerpt_hash=site.hash, scope="current",
                ))
                break

    # dedupe by (fact_id, surface_id)
    seen = set()
    deduped = []
    for loc in locs:
        key = (loc.fact_id, loc.surface_id)
        if key not in seen:
            seen.add(key)
            deduped.append(loc)

    if not deduped:
        log.warning("fact_attribution.unattributable kind=%s excerpts=%s", kind, excerpts[:2])
        return [FindingLocation(
            check=kind, fact_id=None, surface_id="unattributed",
            field_path=None, excerpt_hash=None, scope="structural",
        )]
    return deduped
