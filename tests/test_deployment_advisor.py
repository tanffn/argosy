"""Deployment advisor (P1) — deterministic plan-bound deploy-cash service."""
from argosy.services.deployment_advisor import (
    DeploymentLine,
    DeploymentPlan,
    DeploymentTier,
    EstateTag,
    TIER_NAMES,
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
