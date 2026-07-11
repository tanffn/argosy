"""Discovery dry-powder earmark — deploy-cash Item D money math."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from argosy.services.discovery_reserve import (
    DISCOVERY_RESERVE_LABEL,
    apply_discovery_reserve,
    labeled_exclusion,
    resolve_discovery_reserve_usd,
)


def _cash_doc(*, reserve: dict | None, book_instruments=None):
    cash = SimpleNamespace(
        label="Cash & T-bills (incl. ILS tranche)",
        snapshot_category="Cash",
        sigma_class="cash",
        target_pct=5.68,
        instruments=book_instruments or [],
        agreement="",
        rationale="",
        dissent="",
        discovery_reserve=reserve,
    )
    core = SimpleNamespace(
        label="US broad-market core",
        snapshot_category="Core Equity",
        sigma_class="us_core",
        target_pct=94.32,
        instruments=[
            SimpleNamespace(
                symbol="CSPX", role="primary",
                weight_within_class_pct=100.0, rationale="",
                domicile="IE", exit_triggers=[], review_on=None,
            )
        ],
        agreement="",
        rationale="",
        dissent="",
        discovery_reserve=None,
    )
    return SimpleNamespace(
        schema_version=1,
        basis="full tradeable book",
        anchor_sigma=1.0,
        blended_sigma=1.0,
        nvda_cap_pct=13.0,
        fi_pct=0.0,
        provenance="test",
        classes=[cash, core],
        glide=[],
    )


class TestApplyDiscoveryReserve:
    def test_missing_reserve_unchanged(self):
        d, e = apply_discovery_reserve(cash_total_usd=100_000, reserve_usd=0)
        assert (d, e) == (100_000.0, 0.0)

    def test_normal_subtract(self):
        d, e = apply_discovery_reserve(cash_total_usd=200_000, reserve_usd=59_500)
        assert d == 140_500.0
        assert e == 59_500.0
        assert d + e == 200_000.0

    def test_reserve_exceeds_cash(self):
        d, e = apply_discovery_reserve(cash_total_usd=40_000, reserve_usd=59_500)
        assert d == 0.0
        assert e == 40_000.0

    def test_negative_clamped(self):
        d, e = apply_discovery_reserve(cash_total_usd=-10, reserve_usd=5)
        assert d == 0.0
        assert e == 0.0


class TestResolveFromDoc:
    def test_missing_field_is_zero(self):
        doc = _cash_doc(reserve=None)
        assert resolve_discovery_reserve_usd(doc, book_usd=4_000_000) == 0.0

    def test_usd_at_apply(self):
        doc = _cash_doc(reserve={"usd_at_apply": 59500, "pct_of_book": 1.5})
        assert resolve_discovery_reserve_usd(doc, book_usd=4_000_000) == 59500.0

    def test_pct_of_book_fallback(self):
        doc = _cash_doc(reserve={"pct_of_book": 1.5})
        assert resolve_discovery_reserve_usd(doc, book_usd=4_000_000) == 60_000.0

    def test_label_constant(self):
        assert DISCOVERY_RESERVE_LABEL == (
            "discovery reserve — earmarked, not deployable"
        )
        assert DISCOVERY_RESERVE_LABEL in labeled_exclusion(100.0)


class TestAssembleDeploymentPlanEarmark:
    def test_earmark_excludes_and_labels(self):
        from argosy.services.deployment_advisor import assemble_deployment_plan

        doc = _cash_doc(reserve={"usd_at_apply": 59_500, "pct_of_book": 1.5})
        # Minimal holdings so cash_only_deploy has something to fill against.
        holdings = {"CSPX": 1_000_000.0, "SGOV": 100_000.0}
        cash_total = 200_000.0
        plan = assemble_deployment_plan(
            doc=doc,
            holdings=holdings,
            deploy_amount_usd=cash_total,
            as_of=date(2026, 7, 11),
            use_high_potential=False,
        )
        assert plan.discovery_reserve_usd == 59_500.0
        assert plan.cash_total_usd == cash_total
        assert plan.deploy_amount_usd == pytest.approx(140_500.0)
        # Conservation: deployable attempt + reserve = cash total
        assert (
            plan.deploy_amount_usd + plan.discovery_reserve_usd
            == pytest.approx(cash_total)
        )
        # And deployable = buys + untouched
        assert (
            plan.deployed_total_usd + plan.undeployed_remainder_usd
            == pytest.approx(plan.deploy_amount_usd)
        )
        # Full conservation: buys + untouched + reserve = cash
        assert (
            plan.deployed_total_usd
            + plan.undeployed_remainder_usd
            + plan.discovery_reserve_usd
            == pytest.approx(cash_total)
        )
        assert any(DISCOVERY_RESERVE_LABEL in c for c in plan.caveats)
        assert DISCOVERY_RESERVE_LABEL in plan.note

    def test_plan_without_field_unchanged(self):
        from argosy.services.deployment_advisor import assemble_deployment_plan

        doc = _cash_doc(reserve=None)
        holdings = {"CSPX": 1_000_000.0}
        cash_total = 50_000.0
        plan = assemble_deployment_plan(
            doc=doc,
            holdings=holdings,
            deploy_amount_usd=cash_total,
            as_of=date(2026, 7, 11),
            use_high_potential=False,
        )
        assert plan.discovery_reserve_usd == 0.0
        assert plan.cash_total_usd == cash_total
        assert plan.deploy_amount_usd == cash_total
        assert not any(DISCOVERY_RESERVE_LABEL in c for c in plan.caveats)

    def test_reserve_zero_unchanged(self):
        from argosy.services.deployment_advisor import assemble_deployment_plan

        doc = _cash_doc(reserve={"usd_at_apply": 0, "pct_of_book": 0})
        holdings = {"CSPX": 500_000.0}
        plan = assemble_deployment_plan(
            doc=doc,
            holdings=holdings,
            deploy_amount_usd=10_000.0,
            as_of=date(2026, 7, 11),
            use_high_potential=False,
        )
        assert plan.discovery_reserve_usd == 0.0
        assert plan.deploy_amount_usd == 10_000.0

    def test_reserve_gt_cash_zeros_deployable(self):
        from argosy.services.deployment_advisor import assemble_deployment_plan

        doc = _cash_doc(reserve={"usd_at_apply": 80_000})
        holdings = {"CSPX": 500_000.0}
        plan = assemble_deployment_plan(
            doc=doc,
            holdings=holdings,
            deploy_amount_usd=50_000.0,
            as_of=date(2026, 7, 11),
            use_high_potential=False,
        )
        assert plan.deploy_amount_usd == 0.0
        assert plan.discovery_reserve_usd == 50_000.0
        assert plan.deployed_total_usd == 0.0
        assert any(DISCOVERY_RESERVE_LABEL in c for c in plan.caveats)


class TestPacketDiscoveryReserve:
    def test_packet_nets_and_labels(self):
        from argosy.services.allocation_author.packet import build_decision_packet

        doc = _cash_doc(reserve={"usd_at_apply": 59_500})
        # AllocationInstrument-like objects for the packet menu walk.
        for c in doc.classes:
            c.instruments = [
                SimpleNamespace(
                    symbol="CSPX" if c.sigma_class != "cash" else "SGOV",
                    domicile="IE",
                )
            ]
        packet = build_decision_packet(
            doc=doc,
            holdings_usd={"CSPX": 1_000_000.0, "SGOV": 100_000.0},
            deployable_usd=200_000.0,
            cash_usd=200_000.0,
        )
        assert packet["deployable_usd"] == pytest.approx(140_500.0)
        assert packet["discovery_reserve"]["usd"] == 59_500.0
        assert packet["discovery_reserve"]["label"] == DISCOVERY_RESERVE_LABEL
        assert (
            packet["deployable_usd"] + packet["discovery_reserve"]["usd"]
            == pytest.approx(packet["total_cash_usd"])
        )

    def test_packet_without_field_unchanged(self):
        from argosy.services.allocation_author.packet import build_decision_packet

        doc = _cash_doc(reserve=None)
        for c in doc.classes:
            c.instruments = [SimpleNamespace(symbol="CSPX", domicile="IE")]
        packet = build_decision_packet(
            doc=doc,
            holdings_usd={"CSPX": 1_000_000.0},
            deployable_usd=50_000.0,
        )
        assert packet["deployable_usd"] == 50_000.0
        assert "discovery_reserve" not in packet
