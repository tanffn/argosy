# argosy/quality/rederivation_reviewer.py
"""Re-derive, don't ratify.

The failure that shipped wrong numbers across forty drafts: reviewers RATIFIED a cited
number ("3,000 from plan_doc:rsu_cadence") instead of recomputing it. This reviewer
recomputes every ``Derived`` value in a ``PlanDecisionModel`` from its inputs, BLIND to
the stored value, and BLOCKs on divergence. A stored derived value with NO re-derivation
recipe is itself a BLOCK — no orphan numbers survive.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

from argosy.quality.plan_model import PlanDecisionModel

# A recipe recomputes ONE derived value from a dict of {input_key: value}, blind to the
# stored derived value.
Recipe = Callable[[dict], object]


@dataclass
class RederivationResult:
    ok: bool
    blocks: list[str] = field(default_factory=list)


def _close(a: object, b: object, rel_tol: float, abs_tol: float) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(float(a), float(b), rel_tol=rel_tol, abs_tol=abs_tol)
    return a == b


def review_model(
    model: PlanDecisionModel, recipes: dict[str, Recipe], *,
    rel_tol: float = 1e-6, abs_tol: float = 1.0,
) -> RederivationResult:
    """Recompute each derived value from inputs; BLOCK on divergence or missing recipe."""
    blocks: list[str] = []
    inputs = {k: v.value for k, v in model.inputs.items()}
    for key, d in model.derived.items():
        recipe = recipes.get(key)
        if recipe is None:
            blocks.append(f"{key}: no re-derivation recipe (orphan number) -> BLOCK")
            continue
        try:
            expected = recipe(inputs)
        except Exception as exc:  # noqa: BLE001 — a failed recompute is a BLOCK
            blocks.append(f"{key}: re-derivation failed ({exc!r}) -> BLOCK")
            continue
        if not _close(d.value, expected, rel_tol, abs_tol):
            blocks.append(
                f"{key}: stored {d.value!r} != re-derived {expected!r} -> BLOCK"
            )
    return RederivationResult(ok=not blocks, blocks=blocks)


def standard_recipes() -> dict[str, Recipe]:
    """Recipes for the load-bearing derived values, wrapping the pure derivation fns."""
    from argosy.services.plan_derivation import (
        derive_fi_margin_liquid, derive_nvda_deconcentration,
    )

    def _nvda(field_key: str) -> Recipe:
        def _r(inp: dict):
            return derive_nvda_deconcentration(
                nvda_sh=inp["nvda_sh"], nvda_px_usd=inp["nvda_px_usd"],
                nvda_weight=inp["nvda_weight"], target_w=inp["target_w"], cap=inp["cap"],
            )[field_key].value
        return _r

    return {
        "nvda_target_sh": _nvda("nvda_target_sh"),
        "nvda_sell_sh": _nvda("nvda_sell_sh"),
        "nvda_cap_breach_x": _nvda("nvda_cap_breach_x"),
        "fi_margin_liquid_nis": lambda inp: derive_fi_margin_liquid(
            liquid_nw_nis=inp["liquid_nw_nis"],
            fi_total_capital_nis=inp["fi_total_capital_nis"],
        ).value,
    }
