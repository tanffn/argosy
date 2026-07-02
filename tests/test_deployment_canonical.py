"""Canonical deploy-plan builder — the ONE assembler both /deploy-cash and the
inbox period directive consume, so the buy list can never diverge across surfaces.

It orchestrates the already-tested pieces (assemble_deployment_plan +
run_preflight_for_plan + redirect_overflow_to_diversifiers); these tests pin the
ORCHESTRATION contract (funnel toggle, caveat-append + re-preflight on redirect),
not the pieces' internals (those are covered in test_deployment_advisor /
test_deployment_funnel)."""
from __future__ import annotations

import datetime as _dt

from argosy.services.deployment_funnel.canonical import (
    build_canonical_deploy_plan,
    deploy_plan_to_buy_list,
)


def _doc_with(instruments_by_class):
    from argosy.services.target_allocation_doc import (
        AllocationClassDoc,
        AllocationInstrument,
        TargetAllocationDoc,
    )

    classes = []
    # Targets must sum to ~100 or the allocation engine refuses to size off a
    # non-conserving plan; split evenly across the given classes.
    per_class = round(100.0 / max(1, len(instruments_by_class)), 2)
    for label, instruments in instruments_by_class.items():
        classes.append(
            AllocationClassDoc(
                label=label, snapshot_category=label, sigma_class="us_equity",
                target_pct=per_class,
                instruments=[
                    AllocationInstrument(
                        symbol=s, role="primary", weight_within_class_pct=100.0,
                        rationale="", domicile=d,
                    )
                    for s, d in instruments
                ],
                agreement="", rationale="", dissent="",
            )
        )
    return TargetAllocationDoc(
        anchor_sigma=0.18, blended_sigma=0.16, nvda_cap_pct=13.0, fi_pct=10.0,
        provenance="test", classes=classes, glide=[],
    )


_AS_OF = _dt.date(2026, 7, 2)


def test_buy_list_labels_substitute_by_the_sleeve_it_fills():
    """A top-up line whose symbol differs from the plan ticker (exposure-aware
    substitute) must be labelled by the SLEEVE it fills (via the plan_target cite),
    not fall through to the high-potential label."""
    from argosy.services.deployment_advisor import (
        DeploymentLine,
        DeploymentPlan,
        DeploymentTier,
        EstateTag,
    )

    doc = _doc_with({"International developed (ex-US)": [("EXUS", "IE")]})
    line = DeploymentLine(
        symbol="FWRA", type="ETF", amount_usd=5_000.0, timing="now", is_new=False,
        tier="core", horizon="10yr+",
        estate=EstateTag(domicile="Global", status="estate_safe", note=""),
        cap_note="", net_of_tax_caveat="", rationale="top up FWRA",
        cites=("plan_target:EXUS", "substitute:FWRA"),
    )
    core = DeploymentTier("core", 70.0, (line,))
    empty = lambda n, c: DeploymentTier(n, c, ())
    plan = DeploymentPlan(
        deploy_amount_usd=5_000.0, as_of=_AS_OF,
        tiers=(empty("reserve", 0.0), core, empty("medium", 25.0), empty("high", 5.0)),
        us_situs_exposed_usd=0.0, us_situs_sanctioned_usd=0.0,
        undeployed_remainder_usd=0.0, market_context_age=None, caveats=(), note="",
    )
    rows = deploy_plan_to_buy_list(plan, doc)
    assert rows[0]["instrument"] == "FWRA"
    assert rows[0]["asset_class"] == "International developed (ex-US)"


def test_funnel_disabled_returns_bare_plan_and_no_result():
    """With the funnel off, the builder is a thin pass-through to assemble: it
    returns the plan and a ``None`` result, conservation intact."""
    doc = _doc_with({"US broad-market core": [("CSPX", "IE")]})
    plan, result = build_canonical_deploy_plan(
        doc=doc, holdings={}, cash_usd=10_000.0, deploy_amount_usd=10_000.0,
        as_of=_AS_OF, use_high_potential=False, funnel_enabled=False,
    )
    assert result is None
    assert plan.deploy_amount_usd == 10_000.0
    # Conservation (money-math): everything deployed or explicitly held back.
    assert abs(plan.deployed_total_usd + plan.undeployed_remainder_usd - 10_000.0) <= 0.01


def test_no_doc_returns_empty_plan_and_no_result():
    """No accepted plan → an empty plan (assemble's own guard) and no funnel run —
    never invents instruments."""
    plan, result = build_canonical_deploy_plan(
        doc=None, holdings={}, cash_usd=5_000.0, deploy_amount_usd=5_000.0,
        as_of=_AS_OF, use_high_potential=False, funnel_enabled=True,
    )
    assert result is None
    assert plan.undeployed_remainder_usd == 5_000.0


def test_redirect_note_is_appended_and_preflight_reruns(monkeypatch):
    """When the diversifier redirect fires, the builder appends the redirect note
    as a caveat AND re-runs the preflight against the redirected plan (so the
    surfaced facts describe the plan the user will actually see)."""
    import argosy.services.deployment_funnel.canonical as canon

    doc = _doc_with({"US broad-market core": [("CSPX", "IE")]})

    calls = {"preflight": 0}

    def _fake_preflight(plan, **kw):
        calls["preflight"] += 1
        return object()  # opaque result; the builder only passes it through

    def _fake_redirect(plan, result, doc):
        from dataclasses import replace

        redirected = replace(plan, note="redirected")
        return redirected, "Redirected overflow into diversifiers."

    monkeypatch.setattr(canon, "run_preflight_for_plan", _fake_preflight)
    monkeypatch.setattr(canon, "redirect_overflow_to_diversifiers", _fake_redirect)

    plan, result = build_canonical_deploy_plan(
        doc=doc, holdings={}, cash_usd=10_000.0, deploy_amount_usd=10_000.0,
        as_of=_AS_OF, use_high_potential=False, funnel_enabled=True,
    )

    assert "Redirected overflow into diversifiers." in plan.caveats
    # Preflight runs once before the redirect and once after (against the new plan).
    assert calls["preflight"] == 2


def test_buy_list_projection_covers_every_deployed_dollar():
    """The inbox buy list is a faithful projection of the canonical plan: one row
    per deployed BUY line, carrying instrument / asset_class / amount / rationale,
    and summing to the plan's deployed total (no dollar shown twice or dropped)."""
    doc = _doc_with(
        {
            "US broad-market core": [("CSPX", "IE")],
            "Ex-US developed": [("EXUS", "IE")],
        }
    )
    plan, _ = build_canonical_deploy_plan(
        doc=doc, holdings={}, cash_usd=20_000.0, deploy_amount_usd=20_000.0,
        as_of=_AS_OF, use_high_potential=False, funnel_enabled=False,
    )
    buy_list = deploy_plan_to_buy_list(plan, doc)

    assert buy_list, "expected a non-empty buy list from a non-zero deploy"
    for row in buy_list:
        assert set(row) >= {"instrument", "asset_class", "amount_usd", "rationale"}
        assert row["amount_usd"] > 0
    total = round(sum(r["amount_usd"] for r in buy_list), 2)
    assert abs(total - plan.deployed_total_usd) <= 0.02
    # asset_class is resolved from the plan (the class the instrument fills),
    # not left blank.
    assert all(r["asset_class"] for r in buy_list)
