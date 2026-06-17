# argosy/quality/coherence/surface_registry.py
"""Declarative map: subject_type -> the surfaces it renders on, each with a field
path and a conform method. Names every place a fact appears so the conformer can
reach ALL of them and the verifier can assert coverage. `derived_from` lists sites
whose value is computed from another, so a conform refreshes dependents.

Seeds the draft-45 subjects; extended in Slice 6. Surface ids:
  long_md / medium_md / short_md          -> PlanVersion.horizon_*_md (markdown)
  short_actions_json / medium_actions_json -> PlanVersion.horizon_*_json (actions[])
  dashboard.<field>                        -> computed WealthDashboard field (derived)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ConformMethod = Literal["markdown", "json_field", "derived"]


@dataclass(frozen=True)
class SurfaceSite:
    subject_type: str
    surface_id: str
    field_path: str = ""           # JSON path for json_field; section anchor for markdown
    conform_method: ConformMethod = "markdown"
    derived_from: tuple[str, ...] = ()  # surface_ids this site is computed from


SUBJECT_REGISTRY: dict[str, list[SurfaceSite]] = {
    "rsu_vest_policy": [
        SurfaceSite("rsu_vest_policy", "long_md", "equity_comp", "markdown"),
        SurfaceSite("rsu_vest_policy", "medium_md", "themes.nvda_rsu", "markdown"),
        SurfaceSite("rsu_vest_policy", "short_md", "posture", "markdown"),
        SurfaceSite("rsu_vest_policy", "short_actions_json",
                    "$.actions[?label~='RSU vest']", "json_field"),
    ],
    "sgln_ucits_membership": [
        SurfaceSite("sgln_ucits_membership", "medium_md", "themes.sgln", "markdown"),
        SurfaceSite("sgln_ucits_membership", "short_md", "posture", "markdown"),
        SurfaceSite("sgln_ucits_membership", "short_actions_json",
                    "$.actions[?label~='UCITS dollar-cost']", "json_field"),
    ],
    "retirement_age_headline": [
        SurfaceSite("retirement_age_headline", "long_md", "reconciliation", "markdown"),
        SurfaceSite("retirement_age_headline", "long_md", "monte_carlo", "markdown"),
        SurfaceSite("retirement_age_headline", "long_md", "withdrawal", "markdown"),
        SurfaceSite("retirement_age_headline", "long_md", "client_goals", "markdown"),
        SurfaceSite("retirement_age_headline", "medium_md", "targets", "markdown"),
    ],
    "tranche_execution_gate": [
        SurfaceSite("tranche_execution_gate", "short_md", "tax_plan", "markdown"),
        SurfaceSite("tranche_execution_gate", "short_md", "actions", "markdown"),
        SurfaceSite("tranche_execution_gate", "short_actions_json",
                    "$.actions[?label~='NVDA June tranche']", "json_field"),
    ],
    # FI capital-sufficiency framing (total ₪11.84M vs liquid 'show both'). A framing
    # dispute the live reader surfaces; conformed via a typed marker on the long body.
    "fi_capital_sufficiency": [
        SurfaceSite("fi_capital_sufficiency", "long_md", "capital_sufficiency", "markdown"),
        SurfaceSite("fi_capital_sufficiency", "long_md", "net_worth", "markdown"),
        SurfaceSite("fi_capital_sufficiency", "medium_md", "rationale", "markdown"),
    ],
}


def sites_for_subject(subject_type: str) -> list[SurfaceSite]:
    return list(SUBJECT_REGISTRY.get(subject_type, []))
