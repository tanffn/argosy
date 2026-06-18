from argosy.quality.plan_model import Derived, Input, PlanDecisionModel
from argosy.services.plan_projection import (
    assert_surface_consistency, project_surfaces,
)


def _model():
    m = PlanDecisionModel()
    m.add_input(Input(key="nvda_sh", value=11471, source="ffs_export"))
    m.add_derived(Derived(key="nvda_target_sh", value=2201, formula="f", inputs_used=("nvda_sh",)))
    m.add_derived(Derived(key="nvda_sell_sh", value=9270, formula="f", inputs_used=("nvda_sh",)))
    m.add_derived(Derived(key="fi_margin_liquid_nis", value=-148208, formula="f",
                          inputs_used=("nvda_sh",)))
    return m


def test_projection_is_consistent_across_all_surfaces():
    surfaces = project_surfaces(_model())
    assert set(surfaces) == {"long_md", "dashboard", "short_actions_json", "retirement"}
    assert assert_surface_consistency(surfaces) == []
    assert all(s["nvda_target_sh"] == 2201 for s in surfaces.values())


def test_consistency_check_catches_a_drifted_surface():
    surfaces = project_surfaces(_model())
    surfaces["dashboard"]["nvda_target_sh"] = 3000  # a surface drifts off canonical
    issues = assert_surface_consistency(surfaces)
    assert any("nvda_target_sh" in i for i in issues)
