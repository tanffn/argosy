"""Sleeve-gap layer for Deploy Cash (target % / current % / $-to-close)."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from argosy.services.deployment_funnel.plan_gaps import sleeve_gaps_for_deploy
from argosy.services.target_allocation_doc import (
    AllocationClassDoc,
    AllocationInstrument,
    GlideWaypoint,
    TargetAllocationDoc,
)


def _doc():
    def cls(label, sym, pct):
        return AllocationClassDoc(
            label=label, snapshot_category=label, sigma_class="x",
            target_pct=pct,
            instruments=[AllocationInstrument(
                symbol=sym, role="primary",
                weight_within_class_pct=100.0, domicile="IE",
            )],
        )
    return TargetAllocationDoc(
        schema_version=1, anchor_sigma=0.18, blended_sigma=0.18, nvda_cap_pct=13.0,
        fi_pct=20.0, provenance="t",
        classes=[
            cls("US broad-market core", "CSPX", 40.0),
            cls("Cash & T-bills (incl. ILS tranche)", "IB01", 60.0),
        ],
        glide=[GlideWaypoint(
            quarter=0, date=date(2026, 1, 1),
            composition_pct_by_class={
                "US broad-market core": 40.0,
                "Cash & T-bills (incl. ILS tranche)": 60.0,
            },
        )],
    )


def test_sleeve_gaps_scale_to_cash():
    # Book $100k: 10% core (want 40%) → $30k full gap; 90% cash (want 60%) → overweight.
    snap = SimpleNamespace(positions=[
        SimpleNamespace(symbol="VOO", asset_type="Core Equity", usd_value_k=10.0,
                        details="", location="schwab", currency="USD"),
        SimpleNamespace(symbol="IB01", asset_type="Cash", usd_value_k=90.0,
                        details="", location="leumi", currency="USD"),
    ])
    gaps = sleeve_gaps_for_deploy(doc=_doc(), snapshot=snap, cash_usd=10_000.0)
    assert len(gaps) == 1
    g = gaps[0]
    assert g.asset_class == "US broad-market core"
    assert g.current_target_pct == 10.0
    assert g.proposed_target_pct == 40.0
    # Full gap is $30k; cash is $10k → scale to $10k.
    assert g.blocked_amount_usd == pytest.approx(10_000.0)
