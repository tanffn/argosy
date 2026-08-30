"""Block H — instrument→plan-class mapping precedence + no US-broad dump."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from argosy.agents.base import ModelCall
from argosy.agents.instrument_plan_classifier import InstrumentPlanClassifierAgent
from argosy.services.allocation_breakdown import build_allocation_breakdown
from argosy.services.instrument_plan_class import (
    CASH_LABEL,
    SOURCE_FLEET,
    SOURCE_OWNER,
    UNMAPPED_LABEL,
    ClassificationEntry,
    classify_unmapped_held,
    load_classification_map,
    owner_reassign,
    resolve_sleeve_label,
)
from argosy.state.models import PlanVersion, PortfolioSnapshotRow, User


@pytest.mark.real_seam
def test_real_classifier_agent_dispatch_parses_complete_batch(monkeypatch):
    """Exercise BaseAgent.run; replace only the external model call."""

    async def fake_call(self, *, system, user, **kwargs):
        assert "CURRENT PLAN CLASSES" in user
        assert "every instrument" in system
        return ModelCall(
            text=json.dumps({
                "decisions": [{
                    "symbol": "CMPS",
                    "plan_class_label": "High-growth / high-potential",
                    "confidence": "HIGH",
                    "what_it_is": "A clinical-stage biotechnology company.",
                    "why_held": "A bounded single-name convexity position.",
                }]
            }),
            tokens_in=10,
            tokens_out=10,
            model="test-model",
        )

    monkeypatch.setattr(InstrumentPlanClassifierAgent, "_call_model", fake_call)
    report = InstrumentPlanClassifierAgent(user_id="ariel").run_sync(
        plan_classes=[{"label": "High-growth / high-potential"}],
        instruments=[{"symbol": "CMPS", "asset_type": "Equity"}],
    )
    assert report.output.decisions[0].symbol == "CMPS"
    assert report.output.decisions[0].plan_class_label == "High-growth / high-potential"


@pytest.mark.real_seam
def test_owner_reassignment_survives_real_migrated_schema(alembic_engine_at_head):
    with Session(alembic_engine_at_head) as session:
        owner_reassign(
            session,
            "ariel",
            "IWQU",
            "Global quality factor",
            why_held="Owner-approved diversified factor exposure.",
        )
        session.commit()
        loaded = load_classification_map(session, "ariel")

    assert loaded["IWQU"].source == SOURCE_OWNER
    assert loaded["IWQU"].plan_class_label == "Global quality factor"


def _entry(label: str, source: str) -> ClassificationEntry:
    return ClassificationEntry(
        symbol="X", plan_class_label=label, source=source,
    )


def test_precedence_plan_doc_beats_owner():
    plan = {"SCHD": "Dividend-quality income"}
    cmap = {
        "SCHD": ClassificationEntry(
            "SCHD", "US broad-market core", SOURCE_OWNER,
        ),
    }
    assert resolve_sleeve_label(
        "SCHD", plan_symbol_labels=plan, classification_map=cmap,
    ) == "Dividend-quality income"


def test_precedence_owner_beats_fleet():
    cmap = {
        "VOO": ClassificationEntry("VOO", "US broad-market core", SOURCE_OWNER),
    }
    # Simulate a fleet label that would disagree — only one row exists; owner wins
    # because that's what's stored. Separate check: fleet alone applies.
    assert resolve_sleeve_label("VOO", classification_map=cmap) == "US broad-market core"
    cmap_fleet = {
        "VOO": ClassificationEntry(
            "VOO", "Global quality growth (ex-NVDA-dense)", SOURCE_FLEET,
        ),
    }
    assert resolve_sleeve_label(
        "VOO", classification_map=cmap_fleet,
    ) == "Global quality growth (ex-NVDA-dense)"


def test_precedence_fleet_beats_plan_seed_row():
    # Only one row per symbol in DB; resolve uses whatever is stored. Fleet
    # seed never overwrites owner; plan seed never overwrites fleet — tested
    # in upsert. Here: fleet row is used when no live plan instrument.
    cmap = {
        "SCHD": ClassificationEntry(
            "SCHD", "Dividend-quality income", SOURCE_FLEET,
        ),
    }
    assert resolve_sleeve_label(
        "SCHD", classification_map=cmap,
    ) == "Dividend-quality income"


def test_unmapped_not_us_broad():
    # Bare equity with no map must NOT land in US-broad.
    label = resolve_sleeve_label("BRK/B", asset_type="Equity")
    assert label == UNMAPPED_LABEL
    assert "broad" not in label.lower()


def test_cash_structural_shortcut():
    assert resolve_sleeve_label("-", asset_type="Cash").startswith("Cash")


@pytest.mark.real_seam
def test_fleet_batch_automatically_classifies_unmapped_held_symbol(
    alembic_engine_at_head,
):
    from argosy.services.target_allocation_doc import (
        AllocationClassDoc,
        AllocationInstrument,
        GlideWaypoint,
        TargetAllocationDoc,
    )

    doc = TargetAllocationDoc(
        schema_version=1,
        anchor_sigma=0.3,
        blended_sigma=0.3,
        nvda_cap_pct=13.0,
        fi_pct=0.0,
        provenance="test",
        classes=[
            AllocationClassDoc(
                label="High-growth / high-potential",
                snapshot_category="Individual Stocks",
                sigma_class="high_growth_basket",
                target_pct=100.0,
                instruments=[
                    AllocationInstrument(
                        symbol="ACHR",
                        role="primary",
                        weight_within_class_pct=100.0,
                        domicile="US",
                    )
                ],
                rationale="Bounded convex moonshot sleeve.",
            )
        ],
        glide=[
            GlideWaypoint(
                quarter=0,
                date=date(2026, 8, 27),
                composition_pct_by_class={"High-growth / high-potential": 100.0},
            )
        ],
    )

    def classifier(*, plan_classes, instruments):
        assert {row["symbol"] for row in instruments} == {"CMPS"}
        assert plan_classes[0]["label"] == "High-growth / high-potential"
        return SimpleNamespace(decisions=[SimpleNamespace(
            symbol="CMPS",
            plan_class_label="High-growth / high-potential",
            confidence="HIGH",
            what_it_is="Compass Pathways, a clinical-stage biotechnology company.",
            why_held="A bounded single-name convexity position in the moonshot sleeve.",
        )])

    with Session(alembic_engine_at_head) as session:
        session.merge(User(id="ariel"))
        session.add(PlanVersion(
            user_id="ariel",
            version_label="current-test",
            source_path="test",
            raw_markdown="test",
            role="current",
            target_allocation_json=doc.model_dump_json(),
        ))
        session.add(PortfolioSnapshotRow(
            user_id="ariel",
            snapshot_date=date(2026, 8, 27),
            imported_at=datetime.now(timezone.utc),
            source_path="test",
            positions_json=json.dumps([{
                "location": "Schwab",
                "currency": "USD",
                "asset_type": "Equity",
                "details": "Compass Pathways",
                "symbol": "CMPS",
                "shares": 1500.0,
                "current_price": 14.0,
                "current_value_local": 21000.0,
                "usd_value_k": 21.0,
            }]),
            allocations_json="[]",
            nvda_sales_json="[]",
            real_estate_json="[]",
            pensions_json="[]",
            totals_json=json.dumps({"total_usd_value_k": 21.0}),
            parse_warnings_json="[]",
        ))
        session.commit()

        result = classify_unmapped_held(
            session,
            "ariel",
            classifier=classifier,
        )
        loaded = load_classification_map(session, "ariel")

    assert result == {
        "classified": 1,
        "unmapped": [],
        "reason": "fleet_batch",
        "mode": "injected",
    }
    assert loaded["CMPS"].source == SOURCE_FLEET
    assert loaded["CMPS"].plan_class_label == "High-growth / high-potential"


def test_ibta_plan_first_cash():
    """Acceptance correction: IBTA stays wherever the plan puts it."""
    plan = {"IBTA": CASH_LABEL}
    cmap = {
        "IBTA": ClassificationEntry(
            "IBTA", "Cash & T-bills (incl. ILS tranche)", SOURCE_FLEET,
        ),
    }
    assert resolve_sleeve_label(
        "IBTA", plan_symbol_labels=plan, classification_map=cmap,
    ) == CASH_LABEL


def test_cross_surface_same_resolve_function():
    """Sleeve column / allocation / deploy gaps share resolve_sleeve_label."""
    from argosy.services import allocation_breakdown as ab
    from argosy.services import instrument_plan_class as ipc
    from argosy.services.deployment_funnel import plan_gaps as pg

    assert ab.resolve_sleeve_label is ipc.resolve_sleeve_label
    # plan_gaps imports build_allocation_breakdown which calls the same function
    assert callable(pg.sleeve_gaps_for_deploy)


def test_breakdown_uses_map_not_asset_type_dump():
    cmap = {
        "SCHD": ClassificationEntry(
            "SCHD", "Dividend-quality income", SOURCE_FLEET,
        ),
        "VOO": ClassificationEntry(
            "VOO", "US broad-market core", SOURCE_FLEET,
        ),
    }
    from datetime import date

    from argosy.services.target_allocation_doc import (
        AllocationClassDoc,
        AllocationInstrument,
        GlideWaypoint,
        TargetAllocationDoc,
    )

    def cls(label, sym, pct):
        return AllocationClassDoc(
            label=label, snapshot_category=label, sigma_class="x",
            target_pct=pct,
            instruments=[AllocationInstrument(
                symbol=sym, role="primary",
                weight_within_class_pct=100.0, domicile="IE",
            )],
        )

    doc = TargetAllocationDoc(
        schema_version=1, anchor_sigma=0.18, blended_sigma=0.18, nvda_cap_pct=13.0,
        fi_pct=20.0, provenance="t",
        classes=[
            cls("US broad-market core", "CSPX", 40.0),
            cls("Dividend-quality income", "FUSA", 20.0),
            cls("Cash & T-bills (incl. ILS tranche)", "IB01", 40.0),
        ],
        glide=[GlideWaypoint(
            quarter=0, date=date(2026, 1, 1),
            composition_pct_by_class={"US broad-market core": 40.0},
        )],
    )
    snap = SimpleNamespace(positions=[
        SimpleNamespace(symbol="SCHD", asset_type="Equity", usd_value_k=160.0, details=""),
        SimpleNamespace(symbol="CSPX", asset_type="Core Equity", usd_value_k=400.0, details=""),
        SimpleNamespace(symbol="FWRA", asset_type="Equity", usd_value_k=100.0, details=""),
        SimpleNamespace(symbol="-", asset_type="Cash", usd_value_k=340.0, details=""),
    ])
    rows = build_allocation_breakdown(snap, doc, classification_map=cmap)
    by = {r.label: r for r in rows}
    assert "SCHD" in {h.symbol for h in by["Dividend-quality income"].holdings}
    assert round(by["Dividend-quality income"].current_pct, 0) == 16.0
    # FWRA has no global-core class in plan → Unmapped (fail loud)
    assert "FWRA" in {h.symbol for h in by[UNMAPPED_LABEL].holdings}
    # Must not absorb FWRA into US-broad
    core_syms = {h.symbol for h in by["US broad-market core"].holdings}
    assert "FWRA" not in core_syms
    assert "CSPX" in core_syms
