"""CFP field-coverage smoke tests — Phase 2 expansion.

Asserts the gap-tracker catalog now reflects the CFP Board's "Core
Financial Planning Technologies Questionnaire" intake categories:
estate, risk management/insurance, tax situation, and education
funding. Existing 6 stages keep their entries; 4 new stages (7-10)
each carry at least 3 fields.

These are smoke tests — they don't assert specific field paths beyond
a representative sample. The point is to catch accidental regression
in coverage when the catalog is refactored.
"""

from __future__ import annotations

from argosy.agents.gap_tracker import (
    STAGE_FIELDS,
    STAGE_ORDER,
    STAGE_REQUIRED_FIELDS,
    all_fields,
)


def test_catalog_has_at_least_50_fields() -> None:
    """The CFP-expanded catalog has at least 50 unique fields."""
    fields = all_fields()
    assert len(fields) >= 50, f"expected >=50 fields, got {len(fields)}"


def test_catalog_has_ten_stages_in_order() -> None:
    """Phase 2 added stages 7-10 (CFP expansion). The concentration-
    reduction follow-up added stage_11 (special situations). The order
    tuple drives all_fields() traversal — if it's wrong, gap ordering
    goes off."""
    assert STAGE_ORDER == (
        "stage_1",
        "stage_2",
        "stage_3",
        "stage_4",
        "stage_5",
        "stage_6",
        "stage_7",
        "stage_8",
        "stage_9",
        "stage_10",
        "stage_11",
    )
    assert set(STAGE_FIELDS.keys()) == set(STAGE_ORDER)


def test_each_new_cfp_stage_has_three_or_more_fields() -> None:
    """Every new CFP stage carries at least 3 fields — anything less
    suggests the migration dropped entries on the floor."""
    for stage in ("stage_7", "stage_8", "stage_9", "stage_10"):
        n = len(STAGE_FIELDS[stage])
        assert n >= 3, f"{stage} has only {n} fields (expected >=3)"


def test_stage_required_fields_still_matches_stage_fields() -> None:
    """Backwards-compat shim — STAGE_REQUIRED_FIELDS must equal the
    dotted-path projection of STAGE_FIELDS for every stage. The /turn
    auto-advance and existing tests both depend on this equality."""
    for stage, fields in STAGE_FIELDS.items():
        assert STAGE_REQUIRED_FIELDS[stage] == [f.path for f in fields]
    assert set(STAGE_REQUIRED_FIELDS.keys()) == set(STAGE_FIELDS.keys())


def test_intake_fields_reexports_stage_required_fields_from_gap_tracker() -> None:
    """Phase 2 moved the canonical catalog to gap_tracker. intake_fields
    re-exports STAGE_REQUIRED_FIELDS lazily (PEP 562) for back-compat —
    confirm the two are identity-equal so callers can't drift."""
    from argosy.agents import intake_fields
    from argosy.agents.gap_tracker import STAGE_REQUIRED_FIELDS as GT_SRF

    assert intake_fields.STAGE_REQUIRED_FIELDS is GT_SRF


def test_estate_stage_covers_canonical_documents() -> None:
    """Stage 7 must touch will, trust, POA, healthcare directive,
    beneficiaries — these are the CFP Board's core estate items."""
    paths = {f.path for f in STAGE_FIELDS["stage_7"]}
    assert any("will" in p for p in paths)
    assert any("trust" in p for p in paths)
    assert any("power_of_attorney" in p for p in paths)
    assert any("healthcare_directive" in p for p in paths)
    assert any("beneficiary" in p for p in paths)


def test_insurance_stage_covers_canonical_lines() -> None:
    """Stage 8 must touch life, disability, health, P&C — the CFP risk
    management baseline."""
    paths = {f.path for f in STAGE_FIELDS["stage_8"]}
    assert any("life_insurance" in p for p in paths)
    assert any("disability" in p for p in paths)
    assert any("health_insurance" in p for p in paths)
    assert any("property_casualty" in p for p in paths)


def test_tax_stage_covers_filing_status_and_carryforwards() -> None:
    paths = {f.path for f in STAGE_FIELDS["stage_9"]}
    assert any("filing_status" in p for p in paths)
    assert any("carryforward" in p for p in paths)


def test_education_stage_targets_per_dependent_funding() -> None:
    paths = {f.path for f in STAGE_FIELDS["stage_10"]}
    assert any("education_funding" in p for p in paths)
    assert any("education_savings" in p for p in paths)


def test_israeli_pension_fields_preserved() -> None:
    """Argosy is bicultural — the IL pension catalog stays even though
    the CFP Board is US-centric. Phase 3 follow-up split the single
    `identity.pensions` bucket into three per-vehicle slots whose keys
    mirror the gemelnet adapter's canonical fund-type values
    (kupat_gemel / keren_hishtalmut / kupat_pensia) so adapter snapshots
    can flow straight into the right gap-tracker slot."""
    paths = {f.path for f in STAGE_FIELDS["stage_3"]}
    assert any(p.startswith("identity.pensions.keren_hishtalmut") for p in paths)
    assert any(p.startswith("identity.pensions.kupat_gemel") for p in paths)
    assert any(p.startswith("identity.pensions.kupat_pensia") for p in paths)


def test_must_have_priority_one_fields_present() -> None:
    """Spot-check that the highest-priority CFP fields land at priority=1
    so they get asked first by the gap-driven picker."""
    by_path = {f.path: f for f in all_fields()}

    # CFP must-haves per the brief.
    must_have_paths = (
        "identity.tax_residency",
        "identity.user_citizenship",
        "identity.marital_status",
        "identity.user_employment_gross_annual",
        "identity.brokerage_accounts",
        "goals.retirement_target_year",
        "goals.target_annual_income",
        "identity.life_insurance",
        "identity.tax_filing_status",
        "identity.dependents_count",
    )
    for path in must_have_paths:
        spec = by_path.get(path)
        assert spec is not None, f"missing must-have field {path}"
        assert spec.priority == 1, (
            f"{path} priority={spec.priority}, expected 1 (must-have)"
        )


def test_fields_distributed_across_all_three_sections() -> None:
    """Every section (identity / goals / constraints) carries at least
    one field — sanity check the section assignments didn't all collapse
    to one bucket."""
    sections = {f.section for f in all_fields()}
    assert sections == {"identity", "goals", "constraints"}


def test_freshness_distribution_uses_all_four_bands() -> None:
    """Phase 2 added quarterly-cadence fields (RSU vest, real estate
    P/L, monthly-expenses-as-quarterly). Confirm all four freshness
    bands are represented in the catalog."""
    bands = {f.freshness for f in all_fields()}
    assert bands == {"one_shot", "monthly", "quarterly", "annual"}
