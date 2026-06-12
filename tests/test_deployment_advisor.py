"""Deployment advisor (P1) — deterministic plan-bound deploy-cash service."""
from argosy.services.deployment_advisor import (
    DeploymentLine,
    DeploymentPlan,
    DeploymentTier,
    EstateTag,
    TIER_NAMES,
    DEPLOY_TIER_CAPS,
    classify_tier,
    build_estate_map,
    NET_OF_TAX_CAVEAT,
    cap_note_for,
)


class TestContracts:
    def test_tier_names_are_reserve_core_medium_high_in_carve_order(self):
        # reserve is carved first; then core, medium, high.
        assert TIER_NAMES == ("reserve", "core", "medium", "high")

    def test_deployment_line_holds_the_table_columns(self):
        line = DeploymentLine(
            symbol="CSPX", type="ETF", amount_usd=1000.0, timing="now",
            is_new=False, tier="core", horizon="10yr+",
            estate=EstateTag(domicile="IE", status="estate_safe", note="UCITS"),
            cap_note="fills US broad-market core", net_of_tax_caveat="net of Israeli CGT",
            rationale="gap-fill",
        )
        assert line.symbol == "CSPX"
        assert line.tier == "core"
        assert line.estate.status == "estate_safe"

    def test_tier_total_sums_its_lines(self):
        lines = (
            DeploymentLine("CSPX", "ETF", 600.0, "now", False, "core", "10yr+",
                           EstateTag("IE", "estate_safe", ""), "", "", ""),
            DeploymentLine("EXUS", "ETF", 400.0, "now", True, "core", "10yr+",
                           EstateTag("IE", "estate_safe", ""), "", "", ""),
        )
        tier = DeploymentTier(name="core", cap_pct=70.0, lines=lines)
        assert tier.total_usd == 1000.0

    def test_plan_deployed_total_sums_all_tiers(self):
        core = DeploymentTier("core", 70.0, (
            DeploymentLine("CSPX", "ETF", 1000.0, "now", False, "core", "10yr+",
                           EstateTag("IE", "estate_safe", ""), "", "", ""),
        ))
        empty = lambda n, c: DeploymentTier(n, c, ())
        plan = DeploymentPlan(
            deploy_amount_usd=1000.0, as_of=__import__("datetime").date(2026, 6, 12),
            tiers=(empty("reserve", 0.0), core, empty("medium", 25.0), empty("high", 5.0)),
            us_situs_total_usd=0.0, market_context_age=None, caveats=(), note="",
        )
        assert plan.deployed_total_usd == 1000.0


class TestTierClassification:
    def test_plan_bound_gap_fill_is_core_in_p1(self):
        # Every cash_only_deploy candidate is plan-bound gap-fill -> core.
        assert classify_tier(kind="BUY", symbol="CSPX", is_plan_instrument=True) == "core"
        assert classify_tier(kind="SWAP", symbol="IB01", is_plan_instrument=True) == "core"

    def test_non_plan_instrument_would_be_tactical_but_p1_emits_none(self):
        # A symbol NOT in the canonical plan would be medium (tactical). cash_only_deploy
        # never emits these in P1, but the classifier is honest about the rule.
        assert classify_tier(kind="BUY", symbol="PLTR", is_plan_instrument=False) == "medium"

    def test_tier_caps_are_70_25_5_on_tactical_post_reserve(self):
        assert DEPLOY_TIER_CAPS == {"core": 70.0, "medium": 25.0, "high": 5.0}


def _doc_with(instruments_by_class):
    """Build a minimal TargetAllocationDoc for estate tests."""
    from argosy.services.target_allocation_doc import (
        AllocationClassDoc, AllocationInstrument, TargetAllocationDoc,
    )
    classes = []
    for label, instruments in instruments_by_class.items():
        classes.append(AllocationClassDoc(
            label=label, snapshot_category=label, sigma_class="us_equity",
            target_pct=10.0,
            instruments=[AllocationInstrument(symbol=s, role="primary",
                                              weight_within_class_pct=100.0,
                                              rationale="", domicile=d)
                         for s, d in instruments],
            agreement="", rationale="", dissent="",
        ))
    return TargetAllocationDoc(
        anchor_sigma=0.18, blended_sigma=0.16, nvda_cap_pct=13.0, fi_pct=10.0,
        provenance="test", classes=classes, glide=[],
    )


class TestEstateAnnotation:
    def test_ucits_ie_instrument_is_estate_safe(self):
        doc = _doc_with({"US broad-market core": [("CSPX", "IE")]})
        emap = build_estate_map(doc)
        assert emap["CSPX"].status == "estate_safe"
        assert emap["CSPX"].domicile == "IE"

    def test_nvda_us_situs_is_sanctioned_not_exposed(self):
        doc = _doc_with({"Strategic single-stock (NVDA)": [("NVDA", "US")]})
        emap = build_estate_map(doc)
        assert emap["NVDA"].status == "us_situs_sanctioned"

    def test_unsanctioned_us_domicile_is_exposed_red(self):
        doc = _doc_with({"US growth": [("VOO", "US")]})
        emap = build_estate_map(doc)
        assert emap["VOO"].status == "us_situs_exposed"

    def test_unstamped_domicile_is_unstamped_yellow(self):
        doc = _doc_with({"Mystery": [("XXXX", None)]})
        emap = build_estate_map(doc)
        assert emap["XXXX"].status == "unstamped"


class TestCapAndTaxAnnotation:
    def test_cap_note_names_the_class_the_buy_fills(self):
        doc = _doc_with({"US broad-market core": [("CSPX", "IE")]})
        note = cap_note_for(doc, symbol="CSPX")
        assert "US broad-market core" in note

    def test_cap_note_flags_nvda_against_the_cap(self):
        doc = _doc_with({"Strategic single-stock (NVDA)": [("NVDA", "US")]})
        note = cap_note_for(doc, symbol="NVDA")
        assert "13" in note  # nvda_cap_pct surfaced

    def test_net_of_tax_caveat_is_a_nonempty_static_string(self):
        assert "net" in NET_OF_TAX_CAVEAT.lower()
        assert "tax" in NET_OF_TAX_CAVEAT.lower()
