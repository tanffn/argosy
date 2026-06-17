"""The reviewer must re-derive blind and BLOCK on divergence — the fix for ratifying a
cited number instead of recomputing it."""
from argosy.quality.plan_model import Derived, Input, PlanDecisionModel
from argosy.quality.rederivation_reviewer import review_model, standard_recipes
from argosy.services.plan_derivation import derive_nvda_deconcentration


def _model_with_inputs() -> PlanDecisionModel:
    m = PlanDecisionModel()
    for k, v in {
        "nvda_sh": 11471, "nvda_px_usd": 200.14, "nvda_weight": 0.6251889,
        "target_w": 0.12, "cap": 0.13,
        "liquid_nw_nis": 11687925.80, "fi_total_capital_nis": 11836133.33,
    }.items():
        m.add_input(Input(key=k, value=v, source="resolver"))
    return m


def test_clean_model_passes():
    m = _model_with_inputs()
    for k, d in derive_nvda_deconcentration(
        nvda_sh=11471, nvda_px_usd=200.14, nvda_weight=0.6251889, target_w=0.12, cap=0.13,
    ).items():
        m.add_derived(d)
    res = review_model(m, standard_recipes())
    assert res.ok, res.blocks


def test_tampered_value_blocks():
    # someone "fixes" the target to the inherited 3,000 — the reviewer recomputes 2,201
    m = _model_with_inputs()
    m.add_derived(Derived(key="nvda_target_sh", value=3000,
                          formula="(tampered)", inputs_used=("nvda_sh",)))
    res = review_model(m, standard_recipes())
    assert res.ok is False
    assert any("nvda_target_sh" in b and "2201" in b for b in res.blocks)


def test_orphan_number_without_recipe_blocks():
    m = _model_with_inputs()
    m.add_derived(Derived(key="mystery_number", value=42,
                          formula="?", inputs_used=("nvda_sh",)))
    res = review_model(m, standard_recipes())
    assert res.ok is False
    assert any("mystery_number" in b and "orphan" in b for b in res.blocks)
