"""Tests for the deterministic action-item guidance mapper.

Covers that representative action labels resolve to the right category and that
every result has non-empty, useful how_to + done_when text (no filler / no
fabricated numbers).
"""

from __future__ import annotations

import pytest

from argosy.services.action_item_guidance import (
    GUIDANCE_CATEGORIES,
    guidance_for_action,
)


@pytest.mark.parametrize(
    "label,expected_category",
    [
        # The headline example the user named.
        ("Verify the June 2026 RSU withholding is adequate", "verify_withholding"),
        ("Check §102 tax withheld on the vest", "verify_withholding"),
        ("Verify pension contribution is maxed for the year", "verify_contribution"),
        ("Confirm keren hishtalmut contribution on track", "verify_contribution"),
        ("Verify the cash balance reconciles to Leumi", "verify_check"),
        ("Rebalance NVDA down to target weight", "rebalance_trim_sell"),
        ("Trim the BRK.B overweight", "rebalance_trim_sell"),
        ("Deploy the NVDA-sale cash into the UCITS sleeve", "buy_deploy_allocate"),
        ("Allocate $50k to the growth sleeve", "buy_deploy_allocate"),
        ("Contribute to the IRA before the deadline", "contribute_fund"),
        ("Top up the emergency fund", "contribute_fund"),
        ("Convert USD to NIS for the wedding", "convert_fx"),
        ("Harvest the IBIT tax loss before year-end", "harvest_tax"),
        ("Review the concentration thesis", "review_reassess"),
        ("Do the thing that matches nothing in particular", "generic"),
    ],
)
def test_category_resolution(label, expected_category):
    g = guidance_for_action(label=label)
    assert g.category == expected_category, (label, g.category)


def test_every_category_is_reachable():
    """All declared categories should be produced by some sample label."""
    samples = [
        "Verify RSU withholding",
        "Verify pension contribution",
        "Verify the cash balance",
        "Trim NVDA",
        "Buy the UCITS ETF",
        "Contribute to gemel",
        "Convert USD to NIS",
        "Harvest tax loss",
        "Review thesis",
        "Random unrelated action",
    ]
    produced = {guidance_for_action(label=s).category for s in samples}
    assert produced == set(GUIDANCE_CATEGORIES)


def test_guidance_is_non_empty_and_actionable():
    g = guidance_for_action(
        label="Verify the June 2026 RSU withholding is adequate",
        detail="payslip vs §102 estimate",
    )
    # how_to gives concrete steps and points at a surface; done_when is a bar.
    assert len(g.how_to) > 40
    assert len(g.done_when) > 20
    assert "§102" in g.how_to or "payslip" in g.how_to.lower()


def test_detail_used_as_fallback_for_matching():
    """When the label is generic but the detail carries the keyword."""
    g = guidance_for_action(label="Action #3", detail="rebalance toward target")
    assert g.category == "rebalance_trim_sell"


def test_sell_action_with_withholding_in_detail_is_not_misrouted():
    """A SELL action whose DETAIL mentions 'net-of-tax-withholding' must route
    to sell guidance, not withholding-verification (the label is what matters)."""
    from argosy.services.action_item_guidance import guidance_for_action
    g = guidance_for_action(
        label="Sell the June 17 net-vested NVDA shares and park proceeds in SGOV",
        detail="Sell the net-of-tax-withholding portion of the 729-share vest.",
    )
    assert g.category == "rebalance_trim_sell", g.category


def test_verify_withholding_still_matches_on_label():
    from argosy.services.action_item_guidance import guidance_for_action
    g = guidance_for_action(label="Verify the June 2026 RSU withholding is adequate")
    assert g.category == "verify_withholding", g.category


def test_dca_tranche_routes_to_deploy():
    from argosy.services.action_item_guidance import guidance_for_action
    g = guidance_for_action(
        label="First UCITS dollar-cost-averaging tranche",
        detail="Open the hybrid UCITS dollar-cost-averaging cadence with CSPX / ACWD.",
    )
    assert g.category == "buy_deploy_allocate", g.category
