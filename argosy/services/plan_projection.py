# argosy/services/plan_projection.py
"""Render plan surfaces as PURE projections of the PlanDecisionModel.

Every surface (markdown bodies, dashboard, actions JSON, /retirement) reads the SAME
derived values from the one canonical model — so cross-surface contradiction (the root of
the chart-consistency sessions and the draft-45 "3,000 in target vs … in actions" class)
is impossible by construction. ``assert_surface_consistency`` is the guard that proves no
surface has drifted from the canonical source.
"""
from __future__ import annotations

from argosy.quality.plan_model import PlanDecisionModel

# The shared facts every surface must render identically.
PROJECTED_KEYS = ("nvda_target_sh", "nvda_sell_sh", "fi_margin_liquid_nis")


def _value(model: PlanDecisionModel, key: str):
    if key in model.derived:
        return model.derived[key].value
    if key in model.inputs:
        return model.inputs[key].value
    return None


def project_surfaces(model: PlanDecisionModel) -> dict[str, dict]:
    """Project the canonical model onto each surface. All surfaces source the SAME
    values — there is no per-surface recomputation that could diverge."""
    facts = {k: _value(model, k) for k in PROJECTED_KEYS}
    return {surface: dict(facts) for surface in
            ("long_md", "dashboard", "short_actions_json", "retirement")}


def assert_surface_consistency(surfaces: dict[str, dict]) -> list[str]:
    """Return a list of cross-surface disagreements (empty == consistent)."""
    issues: list[str] = []
    keys = set().union(*(s.keys() for s in surfaces.values())) if surfaces else set()
    for k in sorted(keys):
        vals = {name: s.get(k) for name, s in surfaces.items()}
        if len({repr(v) for v in vals.values()}) > 1:
            issues.append(f"{k}: surfaces disagree -> {vals}")
    return issues
