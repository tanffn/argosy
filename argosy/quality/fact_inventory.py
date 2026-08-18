"""Phase 1a — the run-106 fact inventory.

The explicit table codex required: for each load-bearing run-106 fact, its
``fact_id``, the derivation that owns it (resolver key / TargetAllocationDoc
field / agent), the surfaces it renders on, and the ``site_kind`` per surface.
This is a static map (the addressable substrate), not executable checking.

site_kind per surface is the design's classification: ``template`` /
``structured_field`` re-render deterministically (Slice 3); ``llm_prose`` must
route through the prose editor.
"""
from __future__ import annotations

from dataclasses import dataclass

from argosy.quality.fact_ledger import SiteKind


@dataclass(frozen=True)
class FactSpec:
    """One inventory row: how a fact is derived + where it renders."""

    fact_id: str
    derivation: str                       # "resolver:<key>" | "doc:<field>" | "agent:<role>" | "renderer"
    surfaces: tuple[str, ...]             # surface_ids it appears on
    site_kinds: dict[str, SiteKind]       # surface_id -> site_kind
    note: str = ""


def _spec(fact_id, derivation, site_map, note=""):
    return FactSpec(
        fact_id=fact_id, derivation=derivation,
        surfaces=tuple(site_map), site_kinds=dict(site_map), note=note,
    )


RUN106_FACTS: dict[str, FactSpec] = {
    "retirement.fi_status": _spec(
        "retirement.fi_status", "resolver:retirement.fi_margin_signed_nis",
        {"body": SiteKind.LLM_PROSE, "dashboard": SiteKind.STRUCTURED_FIELD,
         "appendix": SiteKind.TEMPLATE},
        "reached/not-reached + qualifier; finding [0],[1]",
    ),
    "retirement.earliest_safe_age": _spec(
        "retirement.earliest_safe_age", "resolver:retirement.earliest_safe_age",
        {"body": SiteKind.LLM_PROSE, "appendix": SiteKind.TEMPLATE},
        "OFF-MANDATE reading (typical drawdown, spends principal) — Ariel's "
        "ruling 2026-08-18: publish alongside retirement.preservation_age, "
        "NEVER as a single headline; finding [2]; RED-16 (plan-109)",
    ),
    "retirement.preservation_age": _spec(
        "retirement.preservation_age", "resolver:retirement.preservation_age",
        {"body": SiteKind.LLM_PROSE, "appendix": SiteKind.TEMPLATE},
        "MANDATE-SATISFYING reading (capital preservation, no principal "
        "drawdown, per goals_yaml) — Ariel's ruling 2026-08-18: publish "
        "alongside retirement.earliest_safe_age, the pair the age shown if "
        "a surface can hold only one; finding [2]; RED-16 (plan-109)",
    ),
    "retirement.fi_age": _spec(
        "retirement.fi_age", "resolver:retirement.fi_age",
        {"body": SiteKind.LLM_PROSE, "appendix": SiteKind.TEMPLATE},
        "agent-OPINION trajectory marker (withdrawal_sequencer LLM output, "
        "often donor-inherited/stale) — Ariel's ruling 2026-08-18: MUST NEVER "
        "be presented as a retirement age or size a published FIRE bridge; "
        "finding [2]; RED-12/RED-16 (plan-109)",
    ),
    "retirement.bridge_start_age": _spec(
        "retirement.bridge_start_age", "derived:bridge sized from the age it is presented against",
        {"body": SiteKind.LLM_PROSE, "appendix": SiteKind.TEMPLATE},
        "must equal whichever published age (preservation_age or "
        "earliest_safe_age) the bridge is presented against; never fi_age; "
        "finding [2] (net-new fact); RED-12 (plan-109)",
    ),
    "allocation.target_weights": _spec(
        "allocation.target_weights", "doc:classes[].target_pct",
        {"target_allocation_json": SiteKind.STRUCTURED_FIELD,
         "body": SiteKind.TEMPLATE, "appendix": SiteKind.TEMPLATE},
        "IPS instrument map; finding [5]",
    ),
    "allocation.nvda_cap_pct": _spec(
        "allocation.nvda_cap_pct", "doc:nvda_cap_pct",
        {"target_allocation_json": SiteKind.STRUCTURED_FIELD,
         "body": SiteKind.TEMPLATE},
        "Argosy-derived cap; cap-derivation gate",
    ),
    "rsu.net_retention_pct": _spec(
        "rsu.net_retention_pct", "agent:equity_comp",
        {"body": SiteKind.LLM_PROSE, "appendix": SiteKind.STRUCTURED_FIELD},
        "ledger vs equity-comp vs prose; finding [3] (net-new derivation)",
    ),
    "event.rsu_tax_2026_06_17": _spec(
        "event.rsu_tax_2026_06_17", "agent:tax",
        {"body": SiteKind.LLM_PROSE},
        "amount + currency; finding [4] (net-new derivation)",
    ),
    "instrument.SGLN.wrapper_type": _spec(
        "instrument.SGLN.wrapper_type", "doc:classes[].instruments[].domicile",
        {"body": SiteKind.LLM_PROSE, "appendix": SiteKind.STRUCTURED_FIELD},
        "physical-gold ETC, not UCITS; finding [7]",
    ),
}
