from argosy.quality.fx_gate import check_fx_unit_direction
from argosy.quality.gate_types import GateCheck


def test_inverted_fx_value_is_flagged():
    viol = check_fx_unit_direction(plan_text="", fx_usd_nis=0.34)
    assert len(viol) == 1
    assert viol[0].check is GateCheck.FX_UNIT_DIRECTION


def test_plausible_fx_value_is_clean():
    assert check_fx_unit_direction(plan_text="", fx_usd_nis=3.0) == []


def test_text_percent_inverted_is_flagged():
    viol = check_fx_unit_direction(plan_text="USD/NIS 0.34%", fx_usd_nis=None)
    assert len(viol) >= 1
    assert all(v.check is GateCheck.FX_UNIT_DIRECTION for v in viol)


def test_text_plausible_rate_is_clean():
    assert check_fx_unit_direction(plan_text="USD/NIS 3.02", fx_usd_nis=None) == []


def test_text_inverted_of_form_is_flagged():
    viol = check_fx_unit_direction(plan_text="USD/NIS of 0.33", fx_usd_nis=None)
    assert len(viol) == 1


def test_usd_ils_alias_is_scanned():
    viol = check_fx_unit_direction(plan_text="USD/ILS 0.33", fx_usd_nis=None)
    assert len(viol) == 1


def test_no_inputs_is_clean():
    assert check_fx_unit_direction(plan_text="", fx_usd_nis=None) == []
