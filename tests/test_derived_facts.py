from types import SimpleNamespace

import argosy.services.derived_facts as df


class _FakeResolved:
    def __init__(self, d):
        self._d = d

    def get(self, k):
        return SimpleNamespace(value=self._d[k]) if k in self._d else None


_RESOLVED = {
    "concentration.nvda_current_pct": 0.6251889,
    "concentration.nvda_cap_pct": 0.13,
    "portfolio.liquid_net_worth_nis": 11687925.80,
    "retirement.fi_total_capital_nis": 11836133.33,
}


def test_build_derives_locked_numbers(monkeypatch):
    monkeypatch.setattr(
        "argosy.services.plan_numeric_resolver.resolve_plan_numbers",
        lambda *a, **k: _FakeResolved(_RESOLVED),
    )
    monkeypatch.setattr(df, "_latest_nvda", lambda session, user_id: (11471.0, 200.14))
    facts = df.build_derived_facts(object(), user_id="ariel", decision_run_id=1)
    assert facts["nvda_target_sh"] == 2201
    assert facts["nvda_sell_sh"] == 9270
    assert facts["nvda_cap_breach_x"] == 4.81
    assert round(facts["fi_margin_liquid_nis"]) == -148208


def test_render_guidance_forbids_inherited_and_states_derived():
    facts = {
        "nvda_target_w": 0.12, "nvda_target_sh": 2201, "nvda_sell_sh": 9270,
        "nvda_cap_breach_x": 4.81, "fi_margin_liquid_nis": -148208.0,
    }
    g = df.render_derived_facts_guidance(facts)
    assert "FORBIDDEN" in g and "3,000" in g
    assert "2,201" in g and "9,270" in g
    assert "NOT met" in g and "LIQUID" in g


def test_render_partial_eligibility_real_case():
    # 9,230 eligible vs 9,270 needed -> 40-share gap; sell eligible now, season the rest.
    facts = {
        "nvda_target_w": 0.12, "nvda_target_sh": 2201, "nvda_sell_sh": 9270,
        "nvda_cap_breach_x": 4.81, "fi_margin_liquid_nis": -148208.0,
        "nvda_eligible_now_sh": 9230, "nvda_breaking_sh": 1710,
    }
    g = df.render_derived_facts_guidance(facts)
    assert "9,230 capital-track-eligible shares NOW" in g
    assert "40" in g  # the small remainder that must season


def test_render_full_eligibility_now_horizon():
    facts = {
        "nvda_target_w": 0.12, "nvda_target_sh": 2201, "nvda_sell_sh": 9270,
        "nvda_cap_breach_x": 4.81, "fi_margin_liquid_nis": -148208.0,
        "nvda_eligible_now_sh": 9300, "nvda_breaking_sh": 1640,
    }
    g = df.render_derived_facts_guidance(facts)
    assert "Horizon: NOW" in g and "do NOT wait for 2027" in g


def test_render_empty_is_noop():
    assert df.render_derived_facts_guidance(None) == ""
    assert df.render_derived_facts_guidance({}) == ""


def test_build_returns_none_when_resolver_unavailable(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("no resolver")
    monkeypatch.setattr(
        "argosy.services.plan_numeric_resolver.resolve_plan_numbers", _boom,
    )
    assert df.build_derived_facts(object(), user_id="ariel") is None
