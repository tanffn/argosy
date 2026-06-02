"""Phase 3 evidence-contract tests.

Lives at tests/quality/test_plan_synthesizer_evidence.py once Phase 3 lands.
Run: .venv/Scripts/python.exe -m pytest -m "not llm_eval" \
       tests/quality/test_plan_synthesizer_evidence.py -v

Test coverage map (against plan v3.1 §15):
  - test_section_evidence_validators       (5 sub-tests, one per rule)
  - test_content_gate_numeric_support      (re-verifies the gate side
                                            of Phase 3 outputs)
  - test_inference_requires_assumption     (Pydantic side + gate parity)
  - test_synth_section_round_trip          (JSON round-trip preserves
                                            structure + Decimal)
  - test_section_id_must_be_canonical      (rejects unknown section_id)
  - test_fact_text_min_12_chars            (Pydantic catches short text)
  - test_legacy_planSynthesisOutput_with_empty_sections
                                           (backward-compat — sections=[]
                                            is a valid construction)
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

# All imports anchored at the final paste-target file path.
from argosy.agents.plan_synthesizer_types import (
    Assumption,
    Citation,
    FactClaim,
    HorizonSection,
    PlanSynthesisOutput,
    Section,
    SectionEvidence,
    SynthesisInputs,
)
from argosy.quality.canonical_sections import CANONICAL_SECTION_IDS
from argosy.quality.gate_types import GateCheck
from argosy.quality.plan_output_gate import _validate_section_evidence


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok_numeric_fact() -> FactClaim:
    return FactClaim(
        text="NVDA concentration is 22.3 percent of liquid net worth",
        kind="numeric",
        value=Decimal("22.3"),
        unit="%",
        horizon="medium",
    )


def _ok_plan_doc_citation(supports: int = 0) -> Citation:
    return Citation(
        source_kind="plan_doc",
        source_locator="plan_doc:H2:Concentration:L42",
        extract="NVDA concentration is 22.3 percent of liquid net worth",
        supports_fact_index=supports,
    )


def _ok_section(section_id: str = "concentration") -> Section:
    return Section(
        section_id=section_id,
        horizon="medium",
        title="Concentration & Single-Stock Risk",
        body_md=(
            "NVDA exposure remains the dominant single-stock risk; trim "
            "schedule on track."
        ),
        evidence=SectionEvidence(
            facts=[_ok_numeric_fact()],
            source_span=[_ok_plan_doc_citation(supports=0)],
            assumptions=[],
            missing_data=[],
        ),
    )


def _ok_horizon(horizon: str) -> HorizonSection:
    return HorizonSection(
        horizon=horizon,  # type: ignore[arg-type]
        freshness_expected="quarterly",
        status="no_change",
        posture="Hold.",
        targets=[],
        themes=[],
        actions=[],
        speculative_candidates=[],
        deltas_from_prior=[],
        rationale="",
        cited_sources=[],
    )


# ---------------------------------------------------------------------------
# Test 1 — 5 SectionEvidence validators (parametrized by rule)
# ---------------------------------------------------------------------------


class TestSectionEvidenceValidators:
    """One sub-test per Pydantic @model_validator on SectionEvidence."""

    def test_rule_1_empty_facts_and_missing_data_rejected(self) -> None:
        """Validator _facts_or_missing: silent-empty is forbidden."""
        with pytest.raises(ValidationError) as exc:
            SectionEvidence(
                facts=[],
                source_span=[],
                assumptions=[],
                missing_data=[],
            )
        assert "silent empty is forbidden" in str(exc.value)

    def test_rule_1_missing_data_only_is_valid(self) -> None:
        """When facts=[] but missing_data is populated, valid."""
        ev = SectionEvidence(
            facts=[],
            source_span=[],
            assumptions=[],
            missing_data=["no Schwab CSV for 2025-09 RSU grants"],
        )
        assert ev.missing_data == ["no Schwab CSV for 2025-09 RSU grants"]

    def test_rule_2_fact_without_citation_rejected(self) -> None:
        """Validator _every_fact_cited: every fact slot must be cited."""
        f0 = _ok_numeric_fact()
        f1 = FactClaim(
            text="Cash buffer covers eight months of household expenses",
            kind="qualitative",
            value=None,
        )
        with pytest.raises(ValidationError) as exc:
            SectionEvidence(
                facts=[f0, f1],
                # Only fact 0 is cited; fact 1 has nothing
                source_span=[_ok_plan_doc_citation(supports=0)],
                missing_data=[],
            )
        assert "FactClaim[1]" in str(exc.value)
        assert "no Citation" in str(exc.value)

    def test_rule_3_inference_without_assumption_rejected(self) -> None:
        """Validator _inference_requires_assumption."""
        with pytest.raises(ValidationError) as exc:
            SectionEvidence(
                facts=[_ok_numeric_fact()],
                source_span=[
                    Citation(
                        source_kind="inference",
                        source_locator="inference:concentration_drift",
                        extract=None,
                        supports_fact_index=0,
                    )
                ],
                assumptions=[],  # MUST be non-empty for soft sources
                missing_data=[],
            )
        assert "soft sources" in str(exc.value)

    def test_rule_3_assumption_register_also_requires_assumption(self) -> None:
        """``assumption_register`` was a loophole before the codex
        Phase 3 review fix: it was neither concrete (needing extract)
        nor soft (needing Assumption). A fact could be "cited" with
        zero evidence. Now classified soft alongside inference and
        agent_baseline."""
        with pytest.raises(ValidationError) as exc:
            SectionEvidence(
                facts=[_ok_numeric_fact()],
                source_span=[
                    Citation(
                        source_kind="assumption_register",
                        source_locator="assumption_register:real_return_pct",
                        extract=None,
                        supports_fact_index=0,
                    )
                ],
                assumptions=[],  # was the loophole — no longer
                missing_data=[],
            )
        assert "soft sources" in str(exc.value) or "assumption_register" in str(exc.value)

    def test_rule_4_concrete_source_extract_too_short(self) -> None:
        """Validator _concrete_source_extract: extract < 8 chars rejected."""
        with pytest.raises(ValidationError) as exc:
            SectionEvidence(
                facts=[_ok_numeric_fact()],
                source_span=[
                    Citation(
                        source_kind="plan_doc",
                        source_locator="plan_doc:L1",
                        extract="22.3%",  # only 5 chars — too short
                        supports_fact_index=0,
                    )
                ],
                missing_data=[],
            )
        assert "verbatim extract" in str(exc.value)
        assert ">=8" in str(exc.value) or ">8" in str(exc.value) or "8 chars" in str(exc.value)

    def test_rule_5_supports_fact_index_out_of_range(self) -> None:
        """Validator _unique_cite_indices_per_fact: bad index rejected."""
        with pytest.raises(ValidationError) as exc:
            SectionEvidence(
                facts=[_ok_numeric_fact()],
                source_span=[
                    _ok_plan_doc_citation(supports=5),  # only 1 fact exists
                ],
                missing_data=[],
            )
        assert "out of range" in str(exc.value)

    def test_rule_5_duplicate_citation_rejected(self) -> None:
        """Same (source_locator, fact_index) pair twice is rejected."""
        c = _ok_plan_doc_citation(supports=0)
        with pytest.raises(ValidationError) as exc:
            SectionEvidence(
                facts=[_ok_numeric_fact()],
                source_span=[c, c],
                missing_data=[],
            )
        assert "duplicate" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Test 2 — content gate still catches numeric-substring failures
# ---------------------------------------------------------------------------


def test_content_gate_numeric_support() -> None:
    """A Pydantic-valid SectionEvidence whose numeric fact value is NOT
    a substring of the citation extract must be rejected by the content
    gate (`_validate_section_evidence` in plan_output_gate.py).

    This proves the gate is still load-bearing for Phase 3 outputs —
    Pydantic passes (shape OK), gate flags (semantic mismatch)."""
    # Numeric fact value 277000 but extract mentions 250000 instead.
    section = Section(
        section_id="capital_sufficiency",
        horizon="long",
        title="Capital Sufficiency / Goal Funding",
        body_md="Annual savings target stays at 277,000 NIS.",
        evidence=SectionEvidence(
            facts=[
                FactClaim(
                    text="Annual savings target is 277,000 NIS for 2026",
                    kind="numeric",
                    value=Decimal("277000"),
                    unit="NIS",
                )
            ],
            source_span=[
                Citation(
                    source_kind="plan_doc",
                    source_locator="plan_doc:H2:Cashflow:L88",
                    # 250000, not 277000 — gate must catch
                    extract="Target savings of 250,000 NIS per year",
                    supports_fact_index=0,
                )
            ],
            missing_data=[],
        ),
    )
    violations = _validate_section_evidence(section)
    assert any(
        v.check == GateCheck.EVIDENCE_PER_SECTION
        and "277000" in v.detail
        and "not present" in v.detail
        for v in violations
    ), f"expected numeric-substring violation, got: {violations}"


# ---------------------------------------------------------------------------
# Test 3 — inference => assumption (Pydantic side + content-gate parity)
# ---------------------------------------------------------------------------


def test_inference_requires_assumption() -> None:
    """Pydantic side: rejected at construction."""
    with pytest.raises(ValidationError) as exc:
        SectionEvidence(
            facts=[_ok_numeric_fact()],
            source_span=[
                Citation(
                    source_kind="agent_baseline",
                    source_locator="agent:HouseholdBudget:run42",
                    extract=None,
                    supports_fact_index=0,
                )
            ],
            assumptions=[],
            missing_data=[],
        )
    assert "soft sources" in str(exc.value)

    # Once an assumption is provided, construction succeeds.
    ev = SectionEvidence(
        facts=[_ok_numeric_fact()],
        source_span=[
            Citation(
                source_kind="agent_baseline",
                source_locator="agent:HouseholdBudget:run42",
                extract=None,
                supports_fact_index=0,
            )
        ],
        assumptions=[
            Assumption(
                text="HouseholdBudgetAnalyst monthly burn baseline",
                default_value=Decimal("38500"),
                rationale=(
                    "12-month rolling mean from primary checking account"
                ),
            )
        ],
        missing_data=[],
    )
    assert len(ev.assumptions) == 1


# ---------------------------------------------------------------------------
# Test 4 — Pydantic JSON round-trip preserves Section + Decimal
# ---------------------------------------------------------------------------


def test_synth_section_round_trip() -> None:
    """Section serializes via model_dump_json and re-loads identically."""
    s = _ok_section()
    blob = s.model_dump_json()
    reloaded = Section.model_validate_json(blob)
    assert reloaded == s
    # Decimal precision survives the round-trip
    assert isinstance(reloaded.evidence.facts[0].value, Decimal)
    assert reloaded.evidence.facts[0].value == Decimal("22.3")

    # And a full PlanSynthesisOutput with sections=[...] round-trips too.
    out = PlanSynthesisOutput(
        long=_ok_horizon("long"),
        medium=_ok_horizon("medium"),
        short=_ok_horizon("short"),
        inputs=SynthesisInputs(),
        sections=[_ok_section(), _ok_section("ips")],
    )
    blob = out.model_dump_json()
    reloaded_out = PlanSynthesisOutput.model_validate_json(blob)
    assert len(reloaded_out.sections) == 2
    assert {s.section_id for s in reloaded_out.sections} == {"concentration", "ips"}


# ---------------------------------------------------------------------------
# Test 5 — section_id must be canonical
# ---------------------------------------------------------------------------


def test_section_id_must_be_canonical() -> None:
    """Section construction rejects unknown section_id."""
    with pytest.raises(ValidationError) as exc:
        Section(
            section_id="not_a_real_section",
            horizon="medium",
            title="Bogus",
            body_md="...",
            evidence=SectionEvidence(
                facts=[_ok_numeric_fact()],
                source_span=[_ok_plan_doc_citation()],
                missing_data=[],
            ),
        )
    msg = str(exc.value)
    assert "not_a_real_section" in msg
    assert "CANONICAL_SECTION_IDS" in msg

    # Every canonical key is acceptable.
    for sid in CANONICAL_SECTION_IDS:
        _ok_section(sid)  # constructs without raising


# ---------------------------------------------------------------------------
# Test 6 — FactClaim.text must be >=12 chars after strip
# ---------------------------------------------------------------------------


def test_fact_text_min_12_chars() -> None:
    """Pydantic catches short text both via min_length and strip()."""
    # Hard min_length violation
    with pytest.raises(ValidationError):
        FactClaim(text="NVDA", kind="numeric", value=Decimal("22.3"))

    # Padded-short string (min_length=12 lets " " through; strip catches)
    with pytest.raises(ValidationError) as exc:
        FactClaim(text="   NVDA      ", kind="numeric", value=Decimal("22.3"))
    assert ">=12 chars" in str(exc.value) or "12 chars" in str(exc.value)

    # 12-char minimum exactly: accepted
    ok = FactClaim(text="abcdefghijkl", kind="qualitative", value=None)
    assert ok.text == "abcdefghijkl"


# ---------------------------------------------------------------------------
# Test 7 — backward compat: legacy PlanSynthesisOutput with sections=[]
# ---------------------------------------------------------------------------


def test_legacy_planSynthesisOutput_with_empty_sections() -> None:
    """Existing plan_versions rows have no `sections` key in their JSON.
    `sections: list[Section] = Field(default_factory=list)` means
    model_validate succeeds with sections=[] -- no migration needed.

    Also verifies that explicitly constructing with sections=[] is
    legal (the SectionEvidence validators do NOT fire when there ARE
    no sections — they only apply per-section)."""
    # Construct from a JSON blob that omits `sections` entirely.
    legacy_json = PlanSynthesisOutput(
        long=_ok_horizon("long"),
        medium=_ok_horizon("medium"),
        short=_ok_horizon("short"),
        inputs=SynthesisInputs(),
    ).model_dump_json()
    # Strip sections key to simulate a true legacy row
    import json as _json
    legacy_dict = _json.loads(legacy_json)
    legacy_dict.pop("sections", None)
    reloaded = PlanSynthesisOutput.model_validate(legacy_dict)
    assert reloaded.sections == []

    # And explicit sections=[] is also valid.
    out = PlanSynthesisOutput(
        long=_ok_horizon("long"),
        medium=_ok_horizon("medium"),
        short=_ok_horizon("short"),
        inputs=SynthesisInputs(),
        sections=[],
    )
    assert out.sections == []
