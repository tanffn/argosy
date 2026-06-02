"""Phase 4 P0/P1 distillate schema tests — pre-scaffold.

These tests target the MERGED form of ``PlanDistillate`` (i.e. after
the parent agent splices the P0/P1 fields from
``distillate_phase4_models.py`` into ``argosy/agents/plan_distiller_types.py``).

Run from project root after merge:

    .venv/Scripts/python.exe -m pytest -m "not llm_eval" \\
        tmp_review/phase4_exec/test_phase4_distillate.py -v

Until the merge lands, the first three tests are RED (PlanDistillate
has none of the new fields). ``test_legacy_distillate_loads`` and
``test_grant_row_track_literal_rejects_unknown`` go GREEN immediately
because they exercise only the standalone reference classes /
backward-compat path.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from argosy.agents.plan_distiller_types import (
    BridgeRung,
    CashflowPhase,
    CrossBorderPlan,
    ETFRefRow,
    FXStrategyPlan,
    GrantRow,
    MonteCarloGrid,
    PlanDistillate,
    REPlanItem,
    TaxYearRow,
    WithdrawalYearRow,
)
from argosy.agents.plan_synthesizer_types import Assumption
from argosy.quality.canonical_sections import DISTILLATE_FIELD_TO_SECTION_ID


# ---------------------------------------------------------------------------
# Helpers — build a fully-populated P0/P1 distillate
# ---------------------------------------------------------------------------


def _make_full_distillate_kwargs() -> dict:
    """Return PlanDistillate(**kwargs) inputs covering every P0/P1 field."""
    return dict(
        plan_label="JacobsWealthPlan_2026Q2",
        distilled_at_iso="2026-06-02T00:00:00Z",
        # P0
        plan_assumptions=[
            Assumption(
                text="Real return assumption 4% gross of fees",
                default_value=Decimal("0.04"),
                rationale="User's stated baseline in §Cover/Assumptions",
            ),
            Assumption(
                text="Fee drag 30 bps p.a. blended",
                default_value=Decimal("0.0030"),
                rationale="UCITS TER blended across asset classes",
            ),
        ],
        cashflow_phases=[
            CashflowPhase(
                phase_label="kids_leave_home",
                start_age=58,
                end_age=None,
                annual_delta_nis=Decimal("-40000"),
                narrative="Both children independent; food + utilities drop",
                source_locator="plan.md#L412",
            ),
            CashflowPhase(
                phase_label="car_cycle",
                start_age=50,
                end_age=85,
                annual_delta_nis=Decimal("30000"),
                narrative="Car replacement ~₪150k every 5 yr -> 30k/yr smoothed",
                source_locator="plan.md#L455",
            ),
        ],
        equity_comp_grants=[
            GrantRow(
                grant_id="NVDA-2024-RSU-01",
                grant_date=date(2024, 3, 15),
                share_count=Decimal("400"),
                grant_price_usd=Decimal("875.00"),
                vest_schedule="25% per year over 4 years",
                track="102_capital",
                trustee="ESOP Trust",
                holding_clock_end=date(2026, 3, 15),
            ),
        ],
        unmapped_sections=[
            "Spouse insurance review notes",
            "Discretionary giving discussion",
        ],
        # P1
        fi_bridge=[
            BridgeRung(
                rung_label="keren_drawdown_ages_55_60",
                start_age=55,
                end_age=60,
                source_account="keren_hishtalmut",
                annual_nis=Decimal("420000"),
                tax_status="tax_free",
                notes="Withdrawal after 6-year clock; capital-gains rate 0",
            ),
            BridgeRung(
                rung_label="portfolio_drawdown_60_67",
                start_age=60,
                end_age=67,
                source_account="portfolio_drawdown",
                annual_nis=Decimal("480000"),
                tax_status="capital_gains",
                notes="Bridge to Bituach Leumi statutory age",
            ),
        ],
        withdrawal_schedule=[
            WithdrawalYearRow(
                year=2031,
                age=55,
                source_account="keren_hishtalmut",
                gross_nis=Decimal("420000"),
                tax_withheld_nis=Decimal("0"),
                net_nis=Decimal("420000"),
                running_balance_nis=Decimal("2100000"),
            ),
        ],
        monte_carlo_grid=MonteCarloGrid(
            paths=10000,
            success_definition="Portfolio > ₪0 at age 95",
            success_rate=0.92,
            return_assumption_pct=Decimal("4.0"),
            fee_drag_pct=Decimal("0.30"),
            sensitivity_rows=[
                {"return_pct": Decimal("3.0"), "success_rate": Decimal("0.78")},
                {"return_pct": Decimal("5.0"), "success_rate": Decimal("0.98")},
            ],
        ),
        tax_schedule=[
            TaxYearRow(
                year=2031,
                gross_income_nis=Decimal("420000"),
                surtax_band_nis=Decimal("0"),
                effective_rate_pct=Decimal("0.0"),
                marginal_rate_pct=Decimal("0.0"),
                notes="Keren Hishtalmut withdrawal — exempt at threshold",
            ),
        ],
        cross_border=CrossBorderPlan(
            household_us_persons=["spouse"],
            us_situs_exposure_usd=Decimal("0"),
            nra_estate_tail_usd=Decimal("60000"),
            forms_calendar=[
                {"form": "FBAR", "due_date": "2026-04-15", "jurisdiction": "US"},
                {"form": "1040", "due_date": "2026-04-15", "jurisdiction": "US"},
            ],
            pfic_estate_resolution_per_holder={
                "spouse": "IE-domiciled UCITS only; no US-situs ETFs in spouse accounts",
            },
        ),
        real_estate_plan=[
            REPlanItem(
                property_label="primary_residence",
                action="hold",
                action_date=None,
                expected_outcome_nis=Decimal("0"),
                notes="No planned action; mortgage runs to 2035",
            ),
        ],
        fx_strategy=FXStrategyPlan(
            base_currency="NIS",
            target_usd_pct=Decimal("70"),
            conversion_cadence="threshold_driven",
            threshold_rule="convert NIS->USD when NIS/USD < 3.40, in 50k tranches",
            broker="ibkr",
            annual_savings_nis=Decimal("8000"),
        ),
        etf_reference=[
            ETFRefRow(
                asset_class="global_equity",
                ticker="VWRA",
                domicile="IE",
                ter_bps=Decimal("22"),
                estate_safe=True,
                rating="core_hold",
            ),
            ETFRefRow(
                asset_class="us_equity",
                ticker="VOO",
                domicile="US",
                ter_bps=Decimal("3"),
                estate_safe=False,
                rating="legacy_only",
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_distillate_has_all_p0_p1_fields():
    """Instantiate PlanDistillate with every P0/P1 field populated and
    round-trip through model_dump_json -> model_validate_json."""
    kwargs = _make_full_distillate_kwargs()
    distillate = PlanDistillate(**kwargs)

    # Round-trip serialization — Decimal must survive intact.
    payload = distillate.model_dump_json()
    parsed = json.loads(payload)
    reloaded = PlanDistillate.model_validate(parsed)

    # Spot-check a sample of fields across P0/P1 to confirm
    # round-trip didn't drop or coerce.
    assert len(reloaded.plan_assumptions) == 2
    assert reloaded.cashflow_phases[0].annual_delta_nis == Decimal("-40000")
    assert reloaded.equity_comp_grants[0].track == "102_capital"
    assert reloaded.unmapped_sections == [
        "Spouse insurance review notes",
        "Discretionary giving discussion",
    ]
    assert reloaded.fi_bridge[0].source_account == "keren_hishtalmut"
    assert reloaded.monte_carlo_grid is not None
    assert reloaded.monte_carlo_grid.success_rate == pytest.approx(0.92)
    assert reloaded.cross_border is not None
    assert reloaded.cross_border.household_us_persons == ["spouse"]
    assert reloaded.fx_strategy is not None
    assert reloaded.fx_strategy.conversion_cadence == "threshold_driven"
    assert reloaded.etf_reference[1].estate_safe is False


def test_unmapped_sections_default_empty():
    """A PlanDistillate built without the new fields should default
    `unmapped_sections` (and all other P0/P1 lists) to []."""
    distillate = PlanDistillate(
        plan_label="minimal",
        distilled_at_iso="2026-06-02T00:00:00Z",
    )
    assert distillate.unmapped_sections == []
    assert distillate.plan_assumptions == []
    assert distillate.cashflow_phases == []
    assert distillate.equity_comp_grants == []
    assert distillate.fi_bridge == []
    assert distillate.withdrawal_schedule == []
    assert distillate.tax_schedule == []
    assert distillate.real_estate_plan == []
    assert distillate.etf_reference == []
    assert distillate.monte_carlo_grid is None
    assert distillate.cross_border is None
    assert distillate.fx_strategy is None


def test_binding_map_covers_all_new_fields():
    """Every new P0/P1 PlanDistillate field name must appear as a key
    in DISTILLATE_FIELD_TO_SECTION_ID (either bound to a section_id
    or explicitly ungated with value None)."""
    new_field_names = {
        # P0
        "plan_assumptions",
        "cashflow_phases",
        "equity_comp_grants",
        "unmapped_sections",
        # P1
        "fi_bridge",
        "withdrawal_schedule",
        "monte_carlo_grid",
        "tax_schedule",
        "cross_border",
        "real_estate_plan",
        "fx_strategy",
        "etf_reference",
    }
    missing = new_field_names - set(DISTILLATE_FIELD_TO_SECTION_ID.keys())
    assert not missing, (
        f"new distillate fields missing from binding map: {sorted(missing)}. "
        f"Phase 0 binding gate cannot fire on these — synth may silently "
        f"omit the bound section without a violation."
    )


def test_legacy_distillate_loads():
    """A JSON payload using only the original 7-bucket schema must
    load cleanly with the new P0/P1 fields defaulting to empty.
    Backward-compat guarantee: rolling-deploy safety."""
    legacy_payload = {
        "plan_label": "LegacyPlan_2025",
        "distilled_at_iso": "2025-01-15T00:00:00Z",
        "goals": [
            {
                "label": "retirement_target_year",
                "value": "2034",
                "rationale": "user stated",
                "source_section": "Goals",
            }
        ],
        "principles": [
            {
                "label": "ucits_first",
                "rationale": "estate-safety",
                "source_section": "IPS",
            }
        ],
        "risk_priorities": ["concentration", "sequence_of_returns"],
        "decision_rules": [
            {
                "label": "bracket_aware_rsu_sales",
                "rule": "Never sell into 50% marginal band",
                "source_section": "Tax",
            }
        ],
        "targets": [
            {
                "label": "nvda_cap",
                "value": 15.0,
                "unit": "pct_of_portfolio",
                "stated_at": "2025-01-15",
                "revisit_after": "2026-01-15",
                "rationale": "concentration cap",
                "source_section": "IPS",
            }
        ],
        "constraints": [
            {
                "label": "no_consolidate_brokers",
                "detail": "Keep IBKR and Schwab separate",
                "source_section": "Ops",
            }
        ],
        "stress_tolerance": "Can ride 30% drawdown; no panic-sell",
    }
    distillate = PlanDistillate.model_validate(legacy_payload)

    # Legacy fields intact.
    assert distillate.plan_label == "LegacyPlan_2025"
    assert len(distillate.goals) == 1
    assert distillate.goals[0].label == "retirement_target_year"
    assert distillate.risk_priorities == ["concentration", "sequence_of_returns"]
    assert distillate.stress_tolerance.startswith("Can ride")

    # New P0/P1 fields default to empty / None.
    assert distillate.plan_assumptions == []
    assert distillate.cashflow_phases == []
    assert distillate.equity_comp_grants == []
    assert distillate.unmapped_sections == []
    assert distillate.fi_bridge == []
    assert distillate.withdrawal_schedule == []
    assert distillate.tax_schedule == []
    assert distillate.real_estate_plan == []
    assert distillate.etf_reference == []
    assert distillate.monte_carlo_grid is None
    assert distillate.cross_border is None
    assert distillate.fx_strategy is None


def test_grant_row_track_literal_rejects_unknown():
    """Pydantic v2 Literal validation must reject an unknown track
    string at construction time — catches synth-prompt drift early."""
    with pytest.raises(ValidationError) as excinfo:
        GrantRow(
            grant_id="X-1",
            grant_date=date(2024, 1, 1),
            share_count=Decimal("10"),
            vest_schedule="all at once",
            track="bogus",  # not in Literal[...]
        )
    err_text = str(excinfo.value)
    assert "track" in err_text
