"""Tests for the IPS (Investment Policy Statement) derived from the plan."""
from types import SimpleNamespace

import pytest

import argosy.services.ips as ips_mod
from argosy.services.ips import build_ips
from argosy.services.plan_numeric_resolver import ResolvedPlanNumbers, ResolvedValue


def _rv(key: str, value: float, unit: str) -> ResolvedValue:
    return ResolvedValue(
        key=key, value=value, unit=unit, status="resolved",
        source_locator=f"test:{key}",
    )


_FULL = {
    "retirement.return_assumption_pct": _rv("retirement.return_assumption_pct", 4.0, "pct"),
    "retirement.required_real_yield_pct": _rv("retirement.required_real_yield_pct", 3.2, "pct"),
    "concentration.nvda_cap_pct": _rv("concentration.nvda_cap_pct", 13.0, "pct"),
    "concentration.nvda_target_pct": _rv("concentration.nvda_target_pct", 12.0, "pct"),
    "concentration.nvda_current_pct": _rv("concentration.nvda_current_pct", 62.5, "pct"),
    "concentration.nvda_sell_sh": _rv("concentration.nvda_sell_sh", 9270, "shares"),
    "concentration.nvda_eligible_now_sh": _rv("concentration.nvda_eligible_now_sh", 9230, "shares"),
    "retirement.earliest_safe_age": _rv("retirement.earliest_safe_age", 47.0, "age"),
    "retirement.preservation_age": _rv("retirement.preservation_age", 54.0, "age"),
    "retirement.fi_age": _rv("retirement.fi_age", 49.0, "age"),
    "retirement.fi_crossing_year": _rv("retirement.fi_crossing_year", 2027, "year"),
    "retirement.fi_target_nis": _rv("retirement.fi_target_nis", 11_836_133.0, "nis"),
    "retirement.fi_margin_signed_nis": _rv("retirement.fi_margin_signed_nis", -148_208.0, "nis"),
    "portfolio.liquid_net_worth_nis": _rv("portfolio.liquid_net_worth_nis", 11_687_925.0, "nis"),
    "retirement.mc_horizon_age": _rv("retirement.mc_horizon_age", 95.0, "age"),
    "retirement.pension_unlock_age": _rv("retirement.pension_unlock_age", 60.0, "age"),
    "tax.retention_at_vest_pct": _rv("tax.retention_at_vest_pct", 50.0, "pct"),
    "tax.retention_capital_track_pct": _rv("tax.retention_capital_track_pct", 30.0, "pct"),
}


def _fake_plan():
    return SimpleNamespace(
        id=62, decision_run_id=123, target_allocation_json=None,
        accepted_at=None, imported_at=None,
    )


def _patch(monkeypatch, *, plan, resolved, doc=None, ebc=None, facts=None):
    monkeypatch.setattr("argosy.state.queries.get_current_plan", lambda s, u: plan)
    monkeypatch.setattr(
        "argosy.services.plan_numeric_resolver.resolve_plan_numbers",
        lambda *a, **k: resolved,
    )
    monkeypatch.setattr(
        "argosy.services.target_allocation_doc.load_plan_target_allocation",
        lambda pv: doc,
    )
    if ebc is not None:
        monkeypatch.setattr(
            "argosy.services.target_allocation_doc.doc_equity_bond_cash",
            lambda d: ebc,
        )
    monkeypatch.setattr(
        "argosy.services.derived_facts.build_derived_facts",
        lambda *a, **k: facts,
    )


def test_build_ips_none_when_no_current_plan(monkeypatch):
    monkeypatch.setattr("argosy.state.queries.get_current_plan", lambda s, u: None)
    assert build_ips(object(), user_id="ariel") is None


def test_build_ips_none_when_plan_has_no_decision_run(monkeypatch):
    plan = SimpleNamespace(id=1, decision_run_id=None)
    monkeypatch.setattr("argosy.state.queries.get_current_plan", lambda s, u: plan)
    assert build_ips(object(), user_id="ariel") is None


def test_build_ips_none_when_resolver_raises(monkeypatch):
    monkeypatch.setattr("argosy.state.queries.get_current_plan", lambda s, u: _fake_plan())

    def _boom(*a, **k):
        raise RuntimeError("resolver down")

    monkeypatch.setattr(
        "argosy.services.plan_numeric_resolver.resolve_plan_numbers", _boom
    )
    assert build_ips(object(), user_id="ariel") is None


def test_build_ips_full_resolution(monkeypatch):
    resolved = ResolvedPlanNumbers(values=dict(_FULL))
    _patch(monkeypatch, plan=_fake_plan(), resolved=resolved,
           facts={"nvda_breaking_sh": 1710, "nvda_eligible_now_sh": 9230})
    ips = build_ips(object(), user_id="ariel")
    assert ips is not None
    assert ips.nvda_cap_pct.value == 13.0
    assert ips.nvda_cap_pct.status == "resolved"
    assert ips.earliest_safe_age.value == 47.0
    assert ips.retention_capital_track_pct.value == 30.0
    # Stated-policy fields
    assert ips.general_single_name_cap_pct.status == "policy_default"
    assert ips.general_single_name_cap_pct.value == ips_mod.GENERAL_SINGLE_NAME_CAP_PCT
    assert ips.prefer_capital_track is True
    assert ips.ucits_preferred is True
    assert ips.sanctioned_us_situs == ("NVDA",)
    # Lot-derived breaking shares
    assert ips.nvda_breaking_sh.value == 1710
    assert ips.ips_version.startswith("ips-")


def test_build_ips_marks_pending_keys(monkeypatch):
    # Drop a couple of load-bearing keys -> pending + not complete.
    partial = dict(_FULL)
    del partial["concentration.nvda_cap_pct"]
    del partial["retirement.earliest_safe_age"]
    resolved = ResolvedPlanNumbers(values=partial)
    _patch(monkeypatch, plan=_fake_plan(), resolved=resolved, facts=None)
    ips = build_ips(object(), user_id="ariel")
    assert ips is not None
    assert "concentration.nvda_cap_pct" in ips.pending_keys
    assert "retirement.earliest_safe_age" in ips.pending_keys
    assert ips.nvda_cap_pct.value is None
    assert ips.nvda_cap_pct.status == "pending"
    assert ips.is_complete is False


def test_ips_version_stable_and_sensitive(monkeypatch):
    resolved = ResolvedPlanNumbers(values=dict(_FULL))
    _patch(monkeypatch, plan=_fake_plan(), resolved=resolved, facts=None)
    v1 = build_ips(object(), user_id="ariel").ips_version
    v2 = build_ips(object(), user_id="ariel").ips_version
    assert v1 == v2  # unchanged policy -> stable version

    bumped = dict(_FULL)
    bumped["concentration.nvda_cap_pct"] = _rv("concentration.nvda_cap_pct", 12.0, "pct")
    _patch(monkeypatch, plan=_fake_plan(),
           resolved=ResolvedPlanNumbers(values=bumped), facts=None)
    v3 = build_ips(object(), user_id="ariel").ips_version
    assert v3 != v1  # a changed number changes the version


def test_build_ips_sleeve_targets_from_doc(monkeypatch):
    from argosy.services.target_allocation_doc import (
        AllocationClassDoc,
        TargetAllocationDoc,
    )

    doc = TargetAllocationDoc(
        anchor_sigma=0.18, blended_sigma=0.16, nvda_cap_pct=13.0, fi_pct=8.0,
        provenance="test",
        classes=[
            AllocationClassDoc(
                label="US broad-market core", snapshot_category="Core Equity",
                sigma_class="growth", target_pct=40.0, instruments=[],
            ),
            AllocationClassDoc(
                label="Global bonds", snapshot_category="Bonds",
                sigma_class="bonds", target_pct=15.0, instruments=[],
            ),
            AllocationClassDoc(
                label="Cash", snapshot_category="Cash",
                sigma_class="cash", target_pct=5.0, instruments=[],
            ),
        ],
        glide=[],
    )
    resolved = ResolvedPlanNumbers(values=dict(_FULL))
    _patch(monkeypatch, plan=_fake_plan(), resolved=resolved, doc=doc, facts=None)
    ips = build_ips(object(), user_id="ariel")
    assert {s.label for s in ips.sleeve_targets} == {
        "US broad-market core", "Global bonds", "Cash"
    }
    assert ips.equity_target_pct.value == 40.0
    assert ips.bond_target_pct.value == 15.0
    assert ips.cash_target_pct.value == 5.0
