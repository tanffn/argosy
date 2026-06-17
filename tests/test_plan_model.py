import pytest

from argosy.quality.plan_model import (
    Derived, Input, InheritedTargetError, PlanDecisionModel,
)


def test_derived_requires_formula_and_inputs():
    with pytest.raises(InheritedTargetError):
        Derived(key="x", value=3000, formula="", inputs_used=())


def test_derived_rejects_inherited_source():
    # the 3,000-from-spreadsheet class of error: a derived target copied from a prior doc
    with pytest.raises(InheritedTargetError):
        Derived(key="nvda_cadence", value=3000, formula="copied", inputs_used=("x",),
                seeded_from="plan_doc")


def test_derived_valid_with_formula_and_inputs():
    d = Derived(key="nvda_target_sh", value=2201, formula="floor(...)",
                inputs_used=("nvda_sh", "nvda_px_usd"))
    assert d.value == 2201 and d.kind == "derived"


def test_model_rejects_derived_citing_unknown_input():
    m = PlanDecisionModel()
    m.add_input(Input(key="nvda_sh", value=11471, source="ffs_export"))
    with pytest.raises(InheritedTargetError):
        m.add_derived(Derived(key="t", value=1, formula="f", inputs_used=("does_not_exist",)))


def test_model_accepts_derived_with_known_inputs_and_roundtrips():
    m = PlanDecisionModel()
    m.add_input(Input(key="nvda_sh", value=11471, source="ffs_export", as_of="2026-06-12"))
    m.add_derived(Derived(key="nvda_target_sh", value=2201, formula="floor(...)",
                          inputs_used=("nvda_sh",)))
    d = m.to_dict()
    assert d["inputs"]["nvda_sh"]["value"] == 11471
    assert d["derived"]["nvda_target_sh"]["value"] == 2201
